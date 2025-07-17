#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import urllib.parse
from typing import Any, Generator, List, Optional
from urllib.request import (  # type: ignore[attr-defined]
    getproxies_environment,
    proxy_bypass_environment,
)

from netaddr import AddrFormatError, IPAddress, IPSet
from zope.interface import implementer

from twisted.internet import defer
from twisted.internet.endpoints import HostnameEndpoint, wrapClientTLS
from twisted.internet.interfaces import (
    IProtocol,
    IProtocolFactory,
    IReactorCore,
    IStreamClientEndpoint,
)
from twisted.web.client import URI, Agent, HTTPConnectionPool
from twisted.web.http_headers import Headers
from twisted.web.iweb import IAgent, IAgentEndpointFactory, IBodyProducer, IResponse

from synapse.crypto.context_factory import FederationPolicyForHTTPS
from synapse.http import proxyagent
from synapse.http.client import BlocklistingAgentWrapper, BlocklistingReactorWrapper
from synapse.http.connectproxyclient import HTTPConnectProxyEndpoint
from synapse.http.federation.srv_resolver import Server, SrvResolver
from synapse.http.federation.well_known_resolver import WellKnownResolver
from synapse.http.proxyagent import ProxyAgent
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.types import ISynapseReactor
from synapse.util import Clock

logger = logging.getLogger(__name__)


@implementer(IAgent)
class MatrixFederationAgent:
    """An Agent-like thing which provides a `request` method which correctly
    handles resolving matrix server names when using `matrix-federation://`. Handles
    standard https URIs as normal. The `matrix-federation://` scheme is internal to
    Synapse and we purposely want to avoid colliding with the `matrix://` URL scheme
    which is now specced.

    Doesn't implement any retries. (Those are done in MatrixFederationHttpClient.)

    Args:
        reactor: twisted reactor to use for underlying requests

        tls_client_options_factory:
            factory to use for fetching client tls options, or none to disable TLS.

        user_agent:
            The user agent header to use for federation requests.

        ip_allowlist: Allowed IP addresses.

        ip_blocklist: Disallowed IP addresses.

        proxy_reactor: twisted reactor to use for connections to the proxy server
           reactor might have some blocking applied (i.e. for DNS queries),
           but we need unblocked access to the proxy.

        _srv_resolver:
            SrvResolver implementation to use for looking up SRV records. None
            to use a default implementation.

        _well_known_resolver:
            WellKnownResolver to use to perform well-known lookups. None to use a
            default implementation.
    """

    def __init__(
        self,
        server_name: str,
        reactor: ISynapseReactor,
        tls_client_options_factory: Optional[FederationPolicyForHTTPS],
        user_agent: bytes,
        ip_allowlist: Optional[IPSet],
        ip_blocklist: IPSet,
        _srv_resolver: Optional[SrvResolver] = None,
        _well_known_resolver: Optional[WellKnownResolver] = None,
    ):
        """
        Args:
            server_name: Our homeserver name (used to label metrics) (`hs.hostname`).
            reactor
            tls_client_options_factory
            user_agent
            ip_allowlist
            ip_blocklist
            _srv_resolver
            _well_known_resolver
        """

        # proxy_reactor is not blocklisting reactor
        proxy_reactor = reactor

        # We need to use a DNS resolver which filters out blocked IP
        # addresses, to prevent DNS rebinding.
        reactor = BlocklistingReactorWrapper(reactor, ip_allowlist, ip_blocklist)

        self._clock = Clock(reactor)
        self._pool = HTTPConnectionPool(reactor)
        self._pool.retryAutomatically = False
        self._pool.maxPersistentPerHost = 5
        self._pool.cachedConnectionTimeout = 2 * 60

        self._agent = Agent.usingEndpointFactory(
            reactor,
            MatrixHostnameEndpointFactory(
                reactor,
                proxy_reactor,
                tls_client_options_factory,
                _srv_resolver,
            ),
            pool=self._pool,
        )
        self.user_agent = user_agent

        if _well_known_resolver is None:
            _well_known_resolver = WellKnownResolver(
                server_name=server_name,
                reactor=reactor,
                agent=BlocklistingAgentWrapper(
                    ProxyAgent(
                        reactor,
                        proxy_reactor,
                        pool=self._pool,
                        contextFactory=tls_client_options_factory,
                        use_proxy=True,
                    ),
                    ip_blocklist=ip_blocklist,
                ),
                user_agent=self.user_agent,
            )

        self._well_known_resolver = _well_known_resolver

    @defer.inlineCallbacks
    def request(
        self,
        method: bytes,
        uri: bytes,
        headers: Optional[Headers] = None,
        bodyProducer: Optional[IBodyProducer] = None,
    ) -> Generator[defer.Deferred, Any, IResponse]:
        """
        Args:
            method: HTTP method: GET/POST/etc
            uri: Absolute URI to be retrieved
            headers:
                HTTP headers to send with the request, or None to send no extra headers.
            bodyProducer:
                An object which can generate bytes to make up the
                body of this request (for example, the properly encoded contents of
                a file for a file upload).  Or None if the request is to have
                no body.
        Returns:
            A deferred which fires when the header of the response has been received
            (regardless of the response status code). Fails if there is any problem
            which prevents that response from being received (including problems that
            prevent the request from being sent).
        """
        # We use urlparse as that will set `port` to None if there is no
        # explicit port.
        parsed_uri = urllib.parse.urlparse(uri)

        # There must be a valid hostname.
        assert parsed_uri.hostname

        # If this is a matrix-federation:// URI check if the server has delegated matrix
        # traffic using well-known delegation.
        #
        # We have to do this here and not in the endpoint as we need to rewrite
        # the host header with the delegated server name.
        delegated_server = None
        if (
            parsed_uri.scheme == b"matrix-federation"
            and not _is_ip_literal(parsed_uri.hostname)
            and not parsed_uri.port
        ):
            well_known_result = yield defer.ensureDeferred(
                self._well_known_resolver.get_well_known(parsed_uri.hostname)
            )
            delegated_server = well_known_result.delegated_server

        if delegated_server:
            # Ok, the server has delegated matrix traffic to somewhere else, so
            # lets rewrite the URL to replace the server with the delegated
            # server name.
            uri = urllib.parse.urlunparse(
                (
                    parsed_uri.scheme,
                    delegated_server,
                    parsed_uri.path,
                    parsed_uri.params,
                    parsed_uri.query,
                    parsed_uri.fragment,
                )
            )
            parsed_uri = urllib.parse.urlparse(uri)

        # We need to make sure the host header is set to the netloc of the
        # server and that a user-agent is provided.
        if headers is None:
            request_headers = Headers()
        else:
            request_headers = headers.copy()

        if not request_headers.hasHeader(b"host"):
            request_headers.addRawHeader(b"host", parsed_uri.netloc)
        if not request_headers.hasHeader(b"user-agent"):
            request_headers.addRawHeader(b"user-agent", self.user_agent)

        res = yield make_deferred_yieldable(
            self._agent.request(method, uri, request_headers, bodyProducer)
        )

        return res


@implementer(IAgentEndpointFactory)
class MatrixHostnameEndpointFactory:
    """Factory for MatrixHostnameEndpoint for parsing to an Agent."""

    def __init__(
        self,
        reactor: IReactorCore,
        proxy_reactor: IReactorCore,
        tls_client_options_factory: Optional[FederationPolicyForHTTPS],
        srv_resolver: Optional[SrvResolver],
    ):
        self._reactor = reactor
        self._proxy_reactor = proxy_reactor
        self._tls_client_options_factory = tls_client_options_factory

        if srv_resolver is None:
            srv_resolver = SrvResolver()

        self._srv_resolver = srv_resolver

    def endpointForURI(self, parsed_uri: URI) -> "MatrixHostnameEndpoint":
        return MatrixHostnameEndpoint(
            self._reactor,
            self._proxy_reactor,
            self._tls_client_options_factory,
            self._srv_resolver,
            parsed_uri,
        )


@implementer(IStreamClientEndpoint)
class MatrixHostnameEndpoint:
    """An endpoint that resolves matrix-federation:// URLs using Matrix server name
    resolution (i.e. via SRV). Does not check for well-known delegation.

    Args:
        reactor: twisted reactor to use for underlying requests
        proxy_reactor: twisted reactor to use for connections to the proxy server.
           'reactor' might have some blocking applied (i.e. for DNS queries),
           but we need unblocked access to the proxy.
        tls_client_options_factory:
            factory to use for fetching client tls options, or none to disable TLS.
        srv_resolver: The SRV resolver to use
        parsed_uri: The parsed URI that we're wanting to connect to.

    Raises:
        ValueError if the environment variables contain an invalid proxy specification.
        RuntimeError if no tls_options_factory is given for a https connection
    """

    def __init__(
        self,
        reactor: IReactorCore,
        proxy_reactor: IReactorCore,
        tls_client_options_factory: Optional[FederationPolicyForHTTPS],
        srv_resolver: SrvResolver,
        parsed_uri: URI,
    ):
        self._reactor = reactor
        self._parsed_uri = parsed_uri

        # http_proxy is not needed because federation is always over TLS
        proxies = getproxies_environment()
        https_proxy = proxies["https"].encode() if "https" in proxies else None
        self.no_proxy = proxies["no"] if "no" in proxies else None

        # endpoint and credentials to use to connect to the outbound https proxy, if any.
        (
            self._https_proxy_endpoint,
            self._https_proxy_creds,
        ) = proxyagent.http_proxy_endpoint(
            https_proxy,
            proxy_reactor,
            tls_client_options_factory,
        )

        # set up the TLS connection params
        #
        # XXX disabling TLS is really only supported here for the benefit of the
        # unit tests. We should make the UTs cope with TLS rather than having to make
        # the code support the unit tests.

        if tls_client_options_factory is None:
            self._tls_options = None
        else:
            self._tls_options = tls_client_options_factory.get_options(
                self._parsed_uri.host
            )

        self._srv_resolver = srv_resolver

    def connect(
        self, protocol_factory: IProtocolFactory
    ) -> "defer.Deferred[IProtocol]":
        """Implements IStreamClientEndpoint interface"""

        return run_in_background(self._do_connect, protocol_factory)

    async def _do_connect(self, protocol_factory: IProtocolFactory) -> IProtocol:
        first_exception = None

        server_list = await self._resolve_server()

        for server in server_list:
            host = server.host
            port = server.port

            should_skip_proxy = False
            if self.no_proxy is not None:
                should_skip_proxy = proxy_bypass_environment(
                    host.decode(),
                    proxies={"no": self.no_proxy},
                )

            endpoint: IStreamClientEndpoint
            try:
                if self._https_proxy_endpoint and not should_skip_proxy:
                    logger.debug(
                        "Connecting to %s:%i via %s",
                        host.decode("ascii"),
                        port,
                        self._https_proxy_endpoint,
                    )
                    endpoint = HTTPConnectProxyEndpoint(
                        self._reactor,
                        self._https_proxy_endpoint,
                        host,
                        port,
                        proxy_creds=self._https_proxy_creds,
                    )
                else:
                    logger.debug("Connecting to %s:%i", host.decode("ascii"), port)
                    # not using a proxy
                    endpoint = HostnameEndpoint(self._reactor, host, port)
                if self._tls_options:
                    endpoint = wrapClientTLS(self._tls_options, endpoint)
                result = await make_deferred_yieldable(
                    endpoint.connect(protocol_factory)
                )

                return result
            except Exception as e:
                logger.info(
                    "Failed to connect to %s:%i: %s", host.decode("ascii"), port, e
                )
                if not first_exception:
                    first_exception = e

        # We return the first failure because that's probably the most interesting.
        if first_exception:
            raise first_exception

        # This shouldn't happen as we should always have at least one host/port
        # to try and if that doesn't work then we'll have an exception.
        raise Exception("Failed to resolve server %r" % (self._parsed_uri.netloc,))

    async def _resolve_server(self) -> List[Server]:
        """Resolves the server name to a list of hosts and ports to attempt to
        connect to.
        """

        if self._parsed_uri.scheme != b"matrix-federation":
            return [Server(host=self._parsed_uri.host, port=self._parsed_uri.port)]

        # Note: We don't do well-known lookup as that needs to have happened
        # before now, due to needing to rewrite the Host header of the HTTP
        # request.

        # We reparse the URI so that defaultPort is -1 rather than 80
        parsed_uri = urllib.parse.urlparse(self._parsed_uri.toBytes())

        host = parsed_uri.hostname
        port = parsed_uri.port

        # If there is an explicit port or the host is an IP address we bypass
        # SRV lookups and just use the given host/port.
        if port or _is_ip_literal(host):
            return [Server(host, port or 8448)]

        # Check _matrix-fed._tcp SRV record.
        logger.debug("Looking up SRV record for %s", host.decode(errors="replace"))
        server_list = await self._srv_resolver.resolve_service(
            b"_matrix-fed._tcp." + host
        )

        if server_list:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Got %s from SRV lookup for %s",
                    ", ".join(map(str, server_list)),
                    host.decode(errors="replace"),
                )
            return server_list

        # No _matrix-fed._tcp SRV record, fallback to legacy _matrix._tcp SRV record.
        logger.debug(
            "Looking up deprecated SRV record for %s", host.decode(errors="replace")
        )
        server_list = await self._srv_resolver.resolve_service(b"_matrix._tcp." + host)

        if server_list:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "Got %s from deprecated SRV lookup for %s",
                    ", ".join(map(str, server_list)),
                    host.decode(errors="replace"),
                )
            return server_list

        # No SRV records, so we fallback to host and 8448
        logger.debug("No SRV records for %s", host.decode(errors="replace"))
        return [Server(host, 8448)]


def _is_ip_literal(host: bytes) -> bool:
    """Test if the given host name is either an IPv4 or IPv6 literal.

    Args:
        host: The host name to check

    Returns:
        True if the hostname is an IP address literal.
    """

    host_str = host.decode("ascii")

    try:
        IPAddress(host_str)
        return True
    except AddrFormatError:
        return False
