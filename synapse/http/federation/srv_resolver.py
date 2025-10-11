#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import logging
import random
import time
from typing import Any, Callable, Dict, List

import attr

from twisted.internet import defer
from twisted.internet.error import ConnectError
from twisted.names import client, dns
from twisted.names.error import DNSNameError, DNSNotImplementedError, DomainError

from synapse.logging.context import make_deferred_yieldable

logger = logging.getLogger(__name__)

SERVER_CACHE: Dict[bytes, List["Server"]] = {}


@attr.s(auto_attribs=True, slots=True, frozen=True)
class Server:
    """
    Our record of an individual server which can be tried to reach a destination.

    Attributes:
        host: target hostname
        port:
        priority:
        weight:
        expires: when the cache should expire this record - in *seconds* since
            the epoch
    """

    host: bytes
    port: int
    priority: int = 0
    weight: int = 0
    expires: int = 0


def _sort_server_list(server_list: List[Server]) -> List[Server]:
    """Given a list of SRV records sort them into priority order and shuffle
    each priority with the given weight.
    """
    priority_map: Dict[int, List[Server]] = {}

    for server in server_list:
        priority_map.setdefault(server.priority, []).append(server)

    results = []
    for priority in sorted(priority_map):
        servers = priority_map[priority]

        # This algorithms roughly follows the algorithm described in RFC2782,
        # changed to remove an off-by-one error.
        #
        # N.B. Weights can be zero, which means that they should be picked
        # rarely.

        total_weight = sum(s.weight for s in servers)

        # Total weight can become zero if there are only zero weight servers
        # left, which we handle by just shuffling and appending to the results.
        while servers and total_weight:
            target_weight = random.randint(1, total_weight)

            for s in servers:
                target_weight -= s.weight

                if target_weight <= 0:
                    break

            results.append(s)
            servers.remove(s)
            total_weight -= s.weight

        if servers:
            random.shuffle(servers)
            results.extend(servers)

    return results


class SrvResolver:
    """Interface to the dns client to do SRV lookups, with result caching.

    The default resolver in twisted.names doesn't do any caching (it has a CacheResolver,
    but the cache never gets populated), so we add our own caching layer here.

    Args:
        dns_client (twisted.internet.interfaces.IResolver): twisted resolver impl
        cache: cache object
        get_time: clock implementation. Should return seconds since the epoch
    """

    def __init__(
        self,
        dns_client: Any = client,
        cache: Dict[bytes, List[Server]] = SERVER_CACHE,
        get_time: Callable[[], float] = time.time,
    ):
        self._dns_client = dns_client
        self._cache = cache
        self._get_time = get_time

    async def resolve_service(self, service_name: bytes) -> List[Server]:
        """Look up a SRV record

        Args:
            service_name: record to look up

        Returns:
            a list of the SRV records, or an empty list if none found
        """
        now = int(self._get_time())

        if not isinstance(service_name, bytes):
            raise TypeError("%r is not a byte string" % (service_name,))

        cache_entry = self._cache.get(service_name, None)
        if cache_entry:
            if all(s.expires > now for s in cache_entry):
                servers = list(cache_entry)
                return _sort_server_list(servers)

        try:
            answers, _, _ = await make_deferred_yieldable(
                self._dns_client.lookupService(
                    service_name,
                    # This is a sequence of ints that represent the "number of seconds
                    # after which to reissue the query. When the last timeout expires,
                    # the query is considered failed." The default value in Twisted is
                    # `timeout=(1, 3, 11, 45)` (60s total) which is an "arbitrary"
                    # exponential backoff sequence and is too long (see below).
                    #
                    # We want the total timeout to be below the overarching HTTP request
                    # timeout (60s for federation requests) that spurred on this lookup.
                    # This way, we can see the underlying DNS failure and move on
                    # instead of the user ending up with a generic HTTP request timeout.
                    #
                    # Since these DNS queries are done over UDP (unreliable transport),
                    # by it's nature, it's bound to occasionally fail (dropped packets,
                    # etc). We want a list that starts small and re-issues DNS queries
                    # multiple times until we get a response or timeout.
                    timeout=(
                        1,  # Quick retry for packet loss/scenarios
                        3,  # Still reasonable for slow responders
                        3,  # ...
                        3,  # Already catching 99.9% of successful queries at 10s
                        # Final attempt for extreme edge cases.
                        #
                        # TODO: In the future, we could consider removing this extra
                        # time if we don't see complaints. For comparison, The Windows
                        # DNS resolver gives up after 10s using `(1, 1, 2, 4, 2)`, see
                        # https://learn.microsoft.com/en-us/troubleshoot/windows-server/networking/dns-client-resolution-timeouts
                        5,
                    ),
                )
            )
        except DNSNameError:
            # TODO: cache this. We can get the SOA out of the exception, and use
            # the negative-TTL value.
            return []
        except DNSNotImplementedError:
            # For .onion homeservers this is unavailable, just fallback to host:8448
            return []
        except DomainError as e:
            # We failed to resolve the name (other than a NameError)
            # Try something in the cache, else rereaise
            cache_entry = self._cache.get(service_name, None)
            if cache_entry:
                logger.warning(
                    "Failed to resolve %r, falling back to cache. %r", service_name, e
                )
                return list(cache_entry)
            else:
                raise e
        except defer.TimeoutError as e:
            raise defer.TimeoutError(
                f"Could not resolve DNS for SRV record {service_name!r} due to timeout (50s total)"
            ) from e

        if (
            len(answers) == 1
            and answers[0].type == dns.SRV
            and answers[0].payload
            and answers[0].payload.target == dns.Name(b".")
        ):
            raise ConnectError(f"Service {service_name!r} unavailable")

        servers = []

        for answer in answers:
            if answer.type != dns.SRV or not answer.payload:
                continue

            payload = answer.payload

            servers.append(
                Server(
                    host=payload.target.name,
                    port=payload.port,
                    priority=payload.priority,
                    weight=payload.weight,
                    expires=now + answer.ttl,
                )
            )

        self._cache[service_name] = list(servers)
        return _sort_server_list(servers)
