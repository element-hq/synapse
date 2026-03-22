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

import asyncio
import logging
import random
import time
from typing import Any, Callable

import attr

logger = logging.getLogger(__name__)

SERVER_CACHE: dict[bytes, list["Server"]] = {}

# Try to import DNS resolution libraries in order of preference
_dns_resolver: Any = None
try:
    import dns.resolver
    _dns_resolver = "dnspython"
except ImportError:
    pass


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


def _sort_server_list(server_list: list[Server]) -> list[Server]:
    """Given a list of SRV records sort them into priority order and shuffle
    each priority with the given weight.
    """
    priority_map: dict[int, list[Server]] = {}

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
    """Interface to do SRV lookups, with result caching.

    Uses dnspython for DNS resolution if available, otherwise falls back
    to running `dig` via subprocess.

    Args:
        cache: cache object
        get_time: clock implementation. Should return seconds since the epoch
    """

    def __init__(
        self,
        dns_client: Any = None,
        cache: dict[bytes, list[Server]] = SERVER_CACHE,
        get_time: Callable[[], float] = time.time,
    ):
        self._cache = cache
        self._get_time = get_time

    async def resolve_service(self, service_name: bytes) -> list[Server]:
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
            answers = await self._do_lookup(service_name)
        except Exception as e:
            # Try something in the cache, else reraise
            cache_entry = self._cache.get(service_name, None)
            if cache_entry:
                logger.warning(
                    "Failed to resolve %r, falling back to cache. %r", service_name, e
                )
                return list(cache_entry)
            else:
                raise e

        if not answers:
            return []

        servers = []
        for answer in answers:
            servers.append(
                Server(
                    host=answer["host"],
                    port=answer["port"],
                    priority=answer.get("priority", 0),
                    weight=answer.get("weight", 0),
                    expires=now + answer.get("ttl", 300),
                )
            )

        self._cache[service_name] = list(servers)
        return _sort_server_list(servers)

    async def _do_lookup(self, service_name: bytes) -> list[dict]:
        """Perform the actual DNS SRV lookup.

        Returns a list of dicts with keys: host, port, priority, weight, ttl
        """
        name_str = service_name.decode("ascii")

        if _dns_resolver == "dnspython":
            return await self._lookup_dnspython(name_str)
        else:
            return await self._lookup_subprocess(name_str)

    async def _lookup_dnspython(self, name: str) -> list[dict]:
        """Use dnspython to resolve SRV records."""
        loop = asyncio.get_event_loop()

        def _resolve() -> list[dict]:
            try:
                answers = dns.resolver.resolve(name, "SRV")
            except dns.resolver.NXDOMAIN:
                return []
            except dns.resolver.NoAnswer:
                return []
            except dns.resolver.NoNameservers:
                return []

            results = []
            for rdata in answers:
                # Check for the "no service" response (target is ".")
                target = rdata.target.to_text()
                if target == ".":
                    return []

                results.append({
                    "host": rdata.target.to_text(omit_final_dot=True).encode("ascii"),
                    "port": rdata.port,
                    "priority": rdata.priority,
                    "weight": rdata.weight,
                    "ttl": answers.rrset.ttl if answers.rrset else 300,
                })
            return results

        return await loop.run_in_executor(None, _resolve)

    async def _lookup_subprocess(self, name: str) -> list[dict]:
        """Fallback: use `dig` subprocess to resolve SRV records."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "dig", "+short", "SRV", name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except (FileNotFoundError, asyncio.TimeoutError):
            logger.warning("Failed to run 'dig' for SRV lookup of %s", name)
            return []

        results = []
        for line in stdout.decode("ascii", errors="replace").strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 4:
                try:
                    priority = int(parts[0])
                    weight = int(parts[1])
                    port = int(parts[2])
                    target = parts[3].rstrip(".")
                    if target == ".":
                        return []
                    results.append({
                        "host": target.encode("ascii"),
                        "port": port,
                        "priority": priority,
                        "weight": weight,
                        "ttl": 300,  # dig +short doesn't give TTL
                    })
                except (ValueError, IndexError):
                    continue
        return results
