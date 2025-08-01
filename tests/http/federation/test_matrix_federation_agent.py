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
import base64
import logging
import os
from typing import Generator, List, Optional, cast
from unittest.mock import AsyncMock, call, patch

import treq
from netaddr import IPSet
from service_identity import VerificationError
from zope.interface import implementer

from twisted.internet import defer
from twisted.internet._sslverify import ClientTLSOptions, OpenSSLCertificateOptions
from twisted.internet.defer import Deferred
from twisted.internet.endpoints import _WrappingProtocol
from twisted.internet.interfaces import (
    IOpenSSLClientConnectionCreator,
    IProtocolFactory,
)
from twisted.internet.protocol import Factory, Protocol
from twisted.protocols.tls import TLSMemoryBIOProtocol
from twisted.web._newclient import ResponseNeverReceived
from twisted.web.client import Agent
from twisted.web.http import HTTPChannel, Request
from twisted.web.http_headers import Headers
from twisted.web.iweb import IPolicyForHTTPS, IResponse

from synapse.config.homeserver import HomeServerConfig
from synapse.config.server import parse_proxy_config
from synapse.crypto.context_factory import FederationPolicyForHTTPS
from synapse.http.federation.matrix_federation_agent import MatrixFederationAgent
from synapse.http.federation.srv_resolver import Server, SrvResolver
from synapse.http.federation.well_known_resolver import (
    WELL_KNOWN_MAX_SIZE,
    WellKnownResolver,
    _cache_period_from_headers,
)
from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    LoggingContextOrSentinel,
    current_context,
)
from synapse.types import ISynapseReactor
from synapse.util.caches.ttlcache import TTLCache

from tests import unittest
from tests.http import dummy_address, get_test_ca_cert_file, wrap_server_factory_for_tls
from tests.server import FakeTransport, ThreadedMemoryReactorClock
from tests.utils import checked_cast, default_config

logger = logging.getLogger(__name__)


class MatrixFederationAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reactor = ThreadedMemoryReactorClock()

        self.mock_resolver = AsyncMock(spec=SrvResolver)

        config_dict = default_config("test", parse=False)
        config_dict["federation_custom_ca_list"] = [get_test_ca_cert_file()]

        self._config = config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")

        self.tls_factory = FederationPolicyForHTTPS(config)

        self.well_known_cache: TTLCache[bytes, Optional[bytes]] = TTLCache(
            cache_name="test_cache",
            server_name="test_server",
            timer=self.reactor.seconds,
        )
        self.had_well_known_cache: TTLCache[bytes, bool] = TTLCache(
            cache_name="test_cache",
            server_name="test_server",
            timer=self.reactor.seconds,
        )
        self.well_known_resolver = WellKnownResolver(
            server_name="OUR_STUB_HOMESERVER_NAME",
            reactor=self.reactor,
            agent=Agent(self.reactor, contextFactory=self.tls_factory),
            user_agent=b"test-agent",
            well_known_cache=self.well_known_cache,
            had_well_known_cache=self.had_well_known_cache,
        )

    def _make_connection(
        self,
        client_factory: IProtocolFactory,
        ssl: bool = True,
        expected_sni: Optional[bytes] = None,
        tls_sanlist: Optional[List[bytes]] = None,
    ) -> HTTPChannel:
        """Builds a test server, and completes the outgoing client connection
        Args:
            client_factory: the the factory that the
                application is trying to use to make the outbound connection. We will
                invoke it to build the client Protocol

            ssl: If true, we will expect an ssl connection and wrap
                server_factory with a TLSMemoryBIOFactory
                False is set only for when proxy expect http connection.
                Otherwise federation requests use always https.

            expected_sni: the expected SNI value

            tls_sanlist: list of SAN entries for the TLS cert presented by the server.

        Returns:
            the server Protocol returned by server_factory
        """

        # build the test server
        server_factory = _get_test_protocol_factory()
        if ssl:
            server_factory = wrap_server_factory_for_tls(
                server_factory,
                self.reactor,
                tls_sanlist
                or [
                    b"DNS:testserv",
                    b"DNS:target-server",
                    b"DNS:xn--bcher-kva.com",
                    b"IP:1.2.3.4",
                    b"IP:::1",
                ],
            )

        server_protocol = server_factory.buildProtocol(dummy_address)
        assert server_protocol is not None
        # now, tell the client protocol factory to build the client protocol (it will be a
        # _WrappingProtocol, around a TLSMemoryBIOProtocol, around an
        # HTTP11ClientProtocol) and wire the output of said protocol up to the server via
        # a FakeTransport.
        #
        # Normally this would be done by the TCP socket code in Twisted, but we are
        # stubbing that out here.
        # NB: we use a checked_cast here to workaround https://github.com/Shoobx/mypy-zope/issues/91)
        client_protocol = checked_cast(
            _WrappingProtocol, client_factory.buildProtocol(dummy_address)
        )
        client_protocol.makeConnection(
            FakeTransport(server_protocol, self.reactor, client_protocol)
        )

        # tell the server protocol to send its stuff back to the client, too
        server_protocol.makeConnection(
            FakeTransport(client_protocol, self.reactor, server_protocol)
        )

        if ssl:
            assert isinstance(server_protocol, TLSMemoryBIOProtocol)
            # fish the test server back out of the server-side TLS protocol.
            http_protocol = server_protocol.wrappedProtocol
            # grab a hold of the TLS connection, in case it gets torn down
            tls_connection = server_protocol._tlsConnection
        else:
            http_protocol = server_protocol
            tls_connection = None

        assert isinstance(http_protocol, HTTPChannel)
        # give the reactor a pump to get the TLS juices flowing (if needed)
        self.reactor.advance(0)

        # check the SNI
        if expected_sni is not None:
            server_name = tls_connection.get_servername()
            self.assertEqual(
                server_name,
                expected_sni,
                f"Expected SNI {expected_sni!s} but got {server_name!s}",
            )

        return http_protocol

    @defer.inlineCallbacks
    def _make_get_request(
        self, uri: bytes
    ) -> Generator["Deferred[object]", object, IResponse]:
        """
        Sends a simple GET request via the agent, and checks its logcontext management
        """
        with LoggingContext("one") as context:
            fetch_d: Deferred[IResponse] = self.agent.request(b"GET", uri)

            # Nothing happened yet
            self.assertNoResult(fetch_d)

            # should have reset logcontext to the sentinel
            _check_logcontext(SENTINEL_CONTEXT)

            fetch_res: IResponse
            try:
                fetch_res = yield fetch_d  # type: ignore[misc, assignment]
                return fetch_res
            except Exception as e:
                logger.info("Fetch of %s failed: %s", uri.decode("ascii"), e)
                raise
            finally:
                _check_logcontext(context)

    def _handle_well_known_connection(
        self,
        client_factory: IProtocolFactory,
        expected_sni: bytes,
        content: bytes,
        response_headers: Optional[dict] = None,
    ) -> HTTPChannel:
        """Handle an outgoing HTTPs connection: wire it up to a server, check that the
        request is for a .well-known, and send the response.

        Args:
            client_factory: outgoing connection
            expected_sni: SNI that we expect the outgoing connection to send
            content: content to send back as the .well-known
        Returns:
            server impl
        """
        # make the connection for .well-known
        well_known_server = self._make_connection(
            client_factory, expected_sni=expected_sni
        )
        # check the .well-known request and send a response
        self.assertEqual(len(well_known_server.requests), 1)
        request = well_known_server.requests[0]
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"user-agent"), [b"test-agent"]
        )
        self._send_well_known_response(request, content, headers=response_headers or {})
        return well_known_server

    def _send_well_known_response(
        self,
        request: Request,
        content: bytes,
        headers: Optional[dict] = None,
    ) -> None:
        """Check that an incoming request looks like a valid .well-known request, and
        send back the response.
        """
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/.well-known/matrix/server")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])
        # send back a response
        for k, v in (headers or {}).items():
            request.setHeader(k, v)
        request.write(content)
        request.finish()

        self.reactor.pump((0.1,))

    def _make_agent(self) -> MatrixFederationAgent:
        """
        If a proxy server is set, the MatrixFederationAgent must be created again
        because it is created too early during setUp
        """
        return MatrixFederationAgent(
            server_name="OUR_STUB_HOMESERVER_NAME",
            reactor=cast(ISynapseReactor, self.reactor),
            tls_client_options_factory=self.tls_factory,
            user_agent=b"test-agent",  # Note that this is unused since _well_known_resolver is provided.
            ip_allowlist=IPSet(),
            ip_blocklist=IPSet(),
            proxy_config=parse_proxy_config({}),
            _srv_resolver=self.mock_resolver,
            _well_known_resolver=self.well_known_resolver,
        )

    def test_get(self) -> None:
        """happy-path test of a GET request with an explicit port"""
        self._do_get()

    @patch.dict(
        os.environ,
        {"https_proxy": "proxy.com", "no_proxy": "testserv"},
    )
    def test_get_bypass_proxy(self) -> None:
        """test of a GET request with an explicit port and bypass proxy"""
        self._do_get()

    def _do_get(self) -> None:
        """test of a GET request with an explicit port"""
        self.agent = self._make_agent()

        self.reactor.lookups["testserv"] = "1.2.3.4"
        test_d = self._make_get_request(b"matrix-federation://testserv:8448/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"testserv:8448"]
        )
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"user-agent"), [b"test-agent"]
        )
        content = request.content.read()
        self.assertEqual(content, b"")

        # Deferred is still without a result
        self.assertNoResult(test_d)

        # send the headers
        request.responseHeaders.setRawHeaders(b"Content-Type", [b"application/json"])
        request.write("")

        self.reactor.pump((0.1,))

        response = self.successResultOf(test_d)

        # that should give us a Response object
        self.assertEqual(response.code, 200)

        # Send the body
        request.write(b'{ "a": 1 }')
        request.finish()

        self.reactor.pump((0.1,))

        # check it can be read
        json = self.successResultOf(treq.json_content(response))
        self.assertEqual(json, {"a": 1})

    @patch.dict(
        os.environ, {"https_proxy": "http://proxy.com", "no_proxy": "unused.com"}
    )
    def test_get_via_http_proxy(self) -> None:
        """test for federation request through a http proxy"""
        self._do_get_via_proxy(expect_proxy_ssl=False, expected_auth_credentials=None)

    @patch.dict(
        os.environ,
        {"https_proxy": "http://user:pass@proxy.com", "no_proxy": "unused.com"},
    )
    def test_get_via_http_proxy_with_auth(self) -> None:
        """test for federation request through a http proxy with authentication"""
        self._do_get_via_proxy(
            expect_proxy_ssl=False, expected_auth_credentials=b"user:pass"
        )

    @patch.dict(
        os.environ, {"https_proxy": "https://proxy.com", "no_proxy": "unused.com"}
    )
    def test_get_via_https_proxy(self) -> None:
        """test for federation request through a https proxy"""
        self._do_get_via_proxy(expect_proxy_ssl=True, expected_auth_credentials=None)

    @patch.dict(
        os.environ,
        {"https_proxy": "https://user:pass@proxy.com", "no_proxy": "unused.com"},
    )
    def test_get_via_https_proxy_with_auth(self) -> None:
        """test for federation request through a https proxy with authentication"""
        self._do_get_via_proxy(
            expect_proxy_ssl=True, expected_auth_credentials=b"user:pass"
        )

    def _do_get_via_proxy(
        self,
        expect_proxy_ssl: bool = False,
        expected_auth_credentials: Optional[bytes] = None,
    ) -> None:
        """Send a https federation request via an agent and check that it is correctly
            received at the proxy and client. The proxy can use either http or https.
        Args:
            expect_proxy_ssl: True if we expect the request to connect to the proxy via https.
            expected_auth_credentials: credentials we expect to be presented to authenticate at the proxy
        """
        self.agent = self._make_agent()

        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["proxy.com"] = "9.9.9.9"
        test_d = self._make_get_request(b"matrix-federation://testserv:8448/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        # make sure we are connecting to the proxy
        self.assertEqual(host, "9.9.9.9")
        self.assertEqual(port, 1080)

        # make a test server to act as the proxy, and wire up the client
        proxy_server = self._make_connection(
            client_factory,
            ssl=expect_proxy_ssl,
            tls_sanlist=[b"DNS:proxy.com"] if expect_proxy_ssl else None,
            expected_sni=b"proxy.com" if expect_proxy_ssl else None,
        )

        assert isinstance(proxy_server, HTTPChannel)

        # now there should be a pending CONNECT request
        self.assertEqual(len(proxy_server.requests), 1)

        request = proxy_server.requests[0]
        self.assertEqual(request.method, b"CONNECT")
        self.assertEqual(request.path, b"testserv:8448")

        # Check whether auth credentials have been supplied to the proxy
        proxy_auth_header_values = request.requestHeaders.getRawHeaders(
            b"Proxy-Authorization"
        )

        if expected_auth_credentials is not None:
            # Compute the correct header value for Proxy-Authorization
            encoded_credentials = base64.b64encode(expected_auth_credentials)
            expected_header_value = b"Basic " + encoded_credentials

            # Validate the header's value
            self.assertIn(expected_header_value, proxy_auth_header_values)
        else:
            # Check that the Proxy-Authorization header has not been supplied to the proxy
            self.assertIsNone(proxy_auth_header_values)

        # tell the proxy server not to close the connection
        proxy_server.persistent = True

        request.finish()

        # now we make another test server to act as the upstream HTTP server.
        server_ssl_protocol = wrap_server_factory_for_tls(
            _get_test_protocol_factory(),
            self.reactor,
            sanlist=[
                b"DNS:testserv",
                b"DNS:target-server",
                b"DNS:xn--bcher-kva.com",
                b"IP:1.2.3.4",
                b"IP:::1",
            ],
        ).buildProtocol(dummy_address)

        # Tell the HTTP server to send outgoing traffic back via the proxy's transport.
        proxy_server_transport = proxy_server.transport
        assert proxy_server_transport is not None
        server_ssl_protocol.makeConnection(proxy_server_transport)

        # ... and replace the protocol on the proxy's transport with the
        # TLSMemoryBIOProtocol for the test server, so that incoming traffic
        # to the proxy gets sent over to the HTTP(s) server.

        # See also comment at `_do_https_request_via_proxy`
        # in ../test_proxyagent.py for more details
        if expect_proxy_ssl:
            assert isinstance(proxy_server_transport, TLSMemoryBIOProtocol)
            proxy_server_transport.wrappedProtocol = server_ssl_protocol
        else:
            assert isinstance(proxy_server_transport, FakeTransport)
            client_protocol = proxy_server_transport.other
            assert isinstance(client_protocol, Protocol)
            c2s_transport = checked_cast(FakeTransport, client_protocol.transport)
            c2s_transport.other = server_ssl_protocol

        self.reactor.advance(0)

        server_name = server_ssl_protocol._tlsConnection.get_servername()
        expected_sni = b"testserv"
        self.assertEqual(
            server_name,
            expected_sni,
            f"Expected SNI {expected_sni!s} but got {server_name!s}",
        )

        # now there should be a pending request
        http_server = server_ssl_protocol.wrappedProtocol
        assert isinstance(http_server, HTTPChannel)
        self.assertEqual(len(http_server.requests), 1)

        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"testserv:8448"]
        )
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"user-agent"), [b"test-agent"]
        )
        # Check that the destination server DID NOT receive proxy credentials
        self.assertIsNone(request.requestHeaders.getRawHeaders(b"Proxy-Authorization"))
        content = request.content.read()
        self.assertEqual(content, b"")

        # Deferred is still without a result
        self.assertNoResult(test_d)

        # send the headers
        request.responseHeaders.setRawHeaders(b"Content-Type", [b"application/json"])
        request.write("")

        self.reactor.pump((0.1,))

        response = self.successResultOf(test_d)

        # that should give us a Response object
        self.assertEqual(response.code, 200)

        # Send the body
        request.write(b'{ "a": 1 }')
        request.finish()

        self.reactor.pump((0.1,))

        # check it can be read
        json = self.successResultOf(treq.json_content(response))
        self.assertEqual(json, {"a": 1})

    def test_get_ip_address(self) -> None:
        """
        Test the behaviour when the server name contains an explicit IP (with no port)
        """
        self.agent = self._make_agent()

        # there will be a getaddrinfo on the IP
        self.reactor.lookups["1.2.3.4"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://1.2.3.4/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"1.2.3.4"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_ipv6_address(self) -> None:
        """
        Test the behaviour when the server name contains an explicit IPv6 address
        (with no port)
        """
        self.agent = self._make_agent()

        # there will be a getaddrinfo on the IP
        self.reactor.lookups["::1"] = "::1"

        test_d = self._make_get_request(b"matrix-federation://[::1]/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "::1")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"[::1]"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_ipv6_address_with_port(self) -> None:
        """
        Test the behaviour when the server name contains an explicit IPv6 address
        (with explicit port)
        """
        self.agent = self._make_agent()

        # there will be a getaddrinfo on the IP
        self.reactor.lookups["::1"] = "::1"

        test_d = self._make_get_request(b"matrix-federation://[::1]:80/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "::1")
        self.assertEqual(port, 80)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"[::1]:80"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_hostname_bad_cert(self) -> None:
        """
        Test the behaviour when the certificate on the server doesn't match the hostname
        """
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = []
        self.reactor.lookups["testserv1"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv1/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        # fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # attemptdelay on the hostnameendpoint is 0.3, so takes that long before the
        # .well-known request fails.
        self.reactor.pump((0.4,))

        # now there should be two SRV lookups
        self.mock_resolver.resolve_service.assert_has_calls(
            [call(b"_matrix-fed._tcp.testserv1"), call(b"_matrix._tcp.testserv1")]
        )

        # we should fall back to a direct connection
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv1")

        # there should be no requests
        self.assertEqual(len(http_server.requests), 0)

        # ... and the request should have failed
        e = self.failureResultOf(test_d, ResponseNeverReceived)
        failure_reason = e.value.reasons[0]
        self.assertIsInstance(failure_reason.value, VerificationError)

    def test_get_ip_address_bad_cert(self) -> None:
        """
        Test the behaviour when the server name contains an explicit IP, but
        the server cert doesn't cover it
        """
        self.agent = self._make_agent()

        # there will be a getaddrinfo on the IP
        self.reactor.lookups["1.2.3.5"] = "1.2.3.5"

        test_d = self._make_get_request(b"matrix-federation://1.2.3.5/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.5")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=None)

        # there should be no requests
        self.assertEqual(len(http_server.requests), 0)

        # ... and the request should have failed
        e = self.failureResultOf(test_d, ResponseNeverReceived)
        failure_reason = e.value.reasons[0]
        self.assertIsInstance(failure_reason.value, VerificationError)

    def test_get_no_srv_no_well_known(self) -> None:
        """
        Test the behaviour when the server name has no port, no SRV, and no well-known
        """
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = []
        self.reactor.lookups["testserv"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        # fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # attemptdelay on the hostnameendpoint is 0.3, so  takes that long before the
        # .well-known request fails.
        self.reactor.pump((0.4,))

        # now there should be two SRV lookups
        self.mock_resolver.resolve_service.assert_has_calls(
            [call(b"_matrix-fed._tcp.testserv"), call(b"_matrix._tcp.testserv")]
        )

        # we should fall back to a direct connection
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_well_known(self) -> None:
        """Test the behaviour when the .well-known delegates elsewhere"""
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = []
        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["target-server"] = "1::f"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "target-server" }',
        )

        # there should be two SRV lookups
        self.mock_resolver.resolve_service.assert_has_calls(
            [
                call(b"_matrix-fed._tcp.target-server"),
                call(b"_matrix._tcp.target-server"),
            ]
        )

        # now we should get a connection to the target server
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, "1::f")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"target-server"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"target-server"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

        self.assertEqual(self.well_known_cache[b"testserv"], b"target-server")

        # check the cache expires
        self.reactor.pump((48 * 3600,))
        self.well_known_cache.expire()
        self.assertNotIn(b"testserv", self.well_known_cache)

    def test_get_well_known_redirect(self) -> None:
        """Test the behaviour when the server name has no port and no SRV record, but
        the .well-known has a 300 redirect
        """
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = []
        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["target-server"] = "1::f"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        redirect_server = self._make_connection(
            client_factory, expected_sni=b"testserv"
        )

        # send a 302 redirect
        self.assertEqual(len(redirect_server.requests), 1)
        request = redirect_server.requests[0]
        request.redirect(b"https://testserv/even_better_known")
        request.finish()

        self.reactor.pump((0.1,))

        # now there should be another connection
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        well_known_server = self._make_connection(
            client_factory, expected_sni=b"testserv"
        )

        self.assertEqual(len(well_known_server.requests), 1, "No request after 302")
        request = well_known_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/even_better_known")
        request.write(b'{ "m.server": "target-server" }')
        request.finish()

        self.reactor.pump((0.1,))

        # there should be two SRV lookups
        self.mock_resolver.resolve_service.assert_has_calls(
            [
                call(b"_matrix-fed._tcp.target-server"),
                call(b"_matrix._tcp.target-server"),
            ]
        )

        # now we should get a connection to the target server
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1::f")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"target-server"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"target-server"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

        self.assertEqual(self.well_known_cache[b"testserv"], b"target-server")

        # check the cache expires
        self.reactor.pump((48 * 3600,))
        self.well_known_cache.expire()
        self.assertNotIn(b"testserv", self.well_known_cache)

    def test_get_invalid_well_known(self) -> None:
        """
        Test the behaviour when the server name has an *invalid* well-known (and no SRV)
        """
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = []
        self.reactor.lookups["testserv"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory, expected_sni=b"testserv", content=b"NOT JSON"
        )

        # now there should be two SRV lookups
        self.mock_resolver.resolve_service.assert_has_calls(
            [call(b"_matrix-fed._tcp.testserv"), call(b"_matrix._tcp.testserv")]
        )

        # we should fall back to a direct connection
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop()
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_well_known_unsigned_cert(self) -> None:
        """Test the behaviour when the .well-known server presents a cert
        not signed by a CA
        """

        # we use the same test server as the other tests, but use an agent with
        # the config left to the default, which will not trust it (since the
        # presented cert is signed by a test CA)

        self.mock_resolver.resolve_service.return_value = []
        self.reactor.lookups["testserv"] = "1.2.3.4"

        config = default_config("test", parse=True)

        # Build a new agent and WellKnownResolver with a different tls factory
        tls_factory = FederationPolicyForHTTPS(config)
        agent = MatrixFederationAgent(
            server_name="OUR_STUB_HOMESERVER_NAME",
            reactor=self.reactor,
            tls_client_options_factory=tls_factory,
            user_agent=b"test-agent",  # This is unused since _well_known_resolver is passed below.
            ip_allowlist=IPSet(),
            ip_blocklist=IPSet(),
            proxy_config=None,
            _srv_resolver=self.mock_resolver,
            _well_known_resolver=WellKnownResolver(
                server_name="OUR_STUB_HOMESERVER_NAME",
                reactor=cast(ISynapseReactor, self.reactor),
                agent=Agent(self.reactor, contextFactory=tls_factory),
                user_agent=b"test-agent",
                well_known_cache=self.well_known_cache,
                had_well_known_cache=self.had_well_known_cache,
            ),
        )

        test_d = agent.request(b"GET", b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        http_proto = self._make_connection(client_factory, expected_sni=b"testserv")

        # there should be no requests
        self.assertEqual(len(http_proto.requests), 0)

        # and there should be two SRV lookups instead
        self.mock_resolver.resolve_service.assert_has_calls(
            [call(b"_matrix-fed._tcp.testserv"), call(b"_matrix._tcp.testserv")]
        )

    def test_get_hostname_srv(self) -> None:
        """
        Test the behaviour when there is a single SRV record for _matrix-fed.
        """
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = [
            Server(host=b"srvtarget", port=8443)
        ]
        self.reactor.lookups["srvtarget"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # the request for a .well-known will have failed with a DNS lookup error.
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix-fed._tcp.testserv"
        )

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_hostname_srv_legacy(self) -> None:
        """
        Test the behaviour when there is a single SRV record for _matrix.
        """
        self.agent = self._make_agent()

        # Return no entries for the _matrix-fed lookup, and a response for _matrix.
        self.mock_resolver.resolve_service.side_effect = [
            [],
            [Server(host=b"srvtarget", port=8443)],
        ]
        self.reactor.lookups["srvtarget"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # the request for a .well-known will have failed with a DNS lookup error.
        self.mock_resolver.resolve_service.assert_has_calls(
            [call(b"_matrix-fed._tcp.testserv"), call(b"_matrix._tcp.testserv")]
        )

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_well_known_srv(self) -> None:
        """Test the behaviour when the .well-known redirects to a place where there
        is a _matrix-fed SRV record.
        """
        self.agent = self._make_agent()

        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["srvtarget"] = "5.6.7.8"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        self.mock_resolver.resolve_service.return_value = [
            Server(host=b"srvtarget", port=8443)
        ]

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "target-server" }',
        )

        # there should be a SRV lookup
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix-fed._tcp.target-server"
        )

        # now we should get a connection to the target of the SRV record
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, "5.6.7.8")
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"target-server"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"target-server"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_get_well_known_srv_legacy(self) -> None:
        """Test the behaviour when the .well-known redirects to a place where there
        is a _matrix SRV record.
        """
        self.agent = self._make_agent()

        self.reactor.lookups["testserv"] = "1.2.3.4"
        self.reactor.lookups["srvtarget"] = "5.6.7.8"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        # Return no entries for the _matrix-fed lookup, and a response for _matrix.
        self.mock_resolver.resolve_service.side_effect = [
            [],
            [Server(host=b"srvtarget", port=8443)],
        ]

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "target-server" }',
        )

        # there should be two SRV lookups
        self.mock_resolver.resolve_service.assert_has_calls(
            [
                call(b"_matrix-fed._tcp.target-server"),
                call(b"_matrix._tcp.target-server"),
            ]
        )

        # now we should get a connection to the target of the SRV record
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, "5.6.7.8")
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"target-server"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"target-server"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_idna_servername(self) -> None:
        """test the behaviour when the server name has idna chars in"""
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = []

        # the resolver is always called with the IDNA hostname as a native string.
        self.reactor.lookups["xn--bcher-kva.com"] = "1.2.3.4"

        # this is idna for bücher.com
        test_d = self._make_get_request(
            b"matrix-federation://xn--bcher-kva.com/foo/bar"
        )

        # Nothing happened yet
        self.assertNoResult(test_d)

        # No SRV record lookup yet
        self.mock_resolver.resolve_service.assert_not_called()

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        # fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # attemptdelay on the hostnameendpoint is 0.3, so  takes that long before the
        # .well-known request fails.
        self.reactor.pump((0.4,))

        # now there should have been a SRV lookup
        self.mock_resolver.resolve_service.assert_has_calls(
            [
                call(b"_matrix-fed._tcp.xn--bcher-kva.com"),
                call(b"_matrix._tcp.xn--bcher-kva.com"),
            ]
        )

        # We should fall back to port 8448
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 2)
        (host, port, client_factory, _timeout, _bindAddress) = clients[1]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8448)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"xn--bcher-kva.com"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"xn--bcher-kva.com"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_idna_srv_target(self) -> None:
        """test the behaviour when the target of a _matrix-fed SRV record has idna chars"""
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = [
            Server(host=b"xn--trget-3qa.com", port=8443)
        ]  # târget.com
        self.reactor.lookups["xn--trget-3qa.com"] = "1.2.3.4"

        test_d = self._make_get_request(
            b"matrix-federation://xn--bcher-kva.com/foo/bar"
        )

        # Nothing happened yet
        self.assertNoResult(test_d)

        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix-fed._tcp.xn--bcher-kva.com"
        )

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"xn--bcher-kva.com"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"xn--bcher-kva.com"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_idna_srv_target_legacy(self) -> None:
        """test the behaviour when the target of a _matrix SRV record has idna chars"""
        self.agent = self._make_agent()

        # Return no entries for the _matrix-fed lookup, and a response for _matrix.
        self.mock_resolver.resolve_service.side_effect = [
            [],
            [Server(host=b"xn--trget-3qa.com", port=8443)],
        ]  # târget.com
        self.reactor.lookups["xn--trget-3qa.com"] = "1.2.3.4"

        test_d = self._make_get_request(
            b"matrix-federation://xn--bcher-kva.com/foo/bar"
        )

        # Nothing happened yet
        self.assertNoResult(test_d)

        self.mock_resolver.resolve_service.assert_has_calls(
            [
                call(b"_matrix-fed._tcp.xn--bcher-kva.com"),
                call(b"_matrix._tcp.xn--bcher-kva.com"),
            ]
        )

        # Make sure treq is trying to connect
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients[0]
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # make a test server, and wire up the client
        http_server = self._make_connection(
            client_factory, expected_sni=b"xn--bcher-kva.com"
        )

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b"host"), [b"xn--bcher-kva.com"]
        )

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_well_known_cache(self) -> None:
        self.reactor.lookups["testserv"] = "1.2.3.4"

        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        well_known_server = self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            response_headers={b"Cache-Control": b"max-age=1000"},
            content=b'{ "m.server": "target-server" }',
        )

        r = self.successResultOf(fetch_d)
        self.assertEqual(r.delegated_server, b"target-server")

        # close the tcp connection
        well_known_server.loseConnection()

        # repeat the request: it should hit the cache
        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )
        r = self.successResultOf(fetch_d)
        self.assertEqual(r.delegated_server, b"target-server")

        # expire the cache
        self.reactor.pump((1000.0,))

        # now it should connect again
        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )

        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            content=b'{ "m.server": "other-server" }',
        )

        r = self.successResultOf(fetch_d)
        self.assertEqual(r.delegated_server, b"other-server")

    def test_well_known_cache_with_temp_failure(self) -> None:
        """Test that we refetch well-known before the cache expires, and that
        it ignores transient errors.
        """

        self.reactor.lookups["testserv"] = "1.2.3.4"

        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        well_known_server = self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            response_headers={b"Cache-Control": b"max-age=1000"},
            content=b'{ "m.server": "target-server" }',
        )

        r = self.successResultOf(fetch_d)
        self.assertEqual(r.delegated_server, b"target-server")

        # close the tcp connection
        well_known_server.loseConnection()

        # Get close to the cache expiry, this will cause the resolver to do
        # another lookup.
        self.reactor.pump((900.0,))

        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )

        # The resolver may retry a few times, so fonx all requests that come along
        attempts = 0
        while self.reactor.tcpClients:
            clients = self.reactor.tcpClients
            (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)

            attempts += 1

            # fonx the connection attempt, this will be treated as a temporary
            # failure.
            client_factory.clientConnectionFailed(None, Exception("nope"))

            # There's a few sleeps involved, so we have to pump the reactor a
            # bit.
            self.reactor.pump((1.0, 1.0))

        # We expect to see more than one attempt as there was previously a valid
        # well known.
        self.assertGreater(attempts, 1)

        # Resolver should return cached value, despite the lookup failing.
        r = self.successResultOf(fetch_d)
        self.assertEqual(r.delegated_server, b"target-server")

        # Expire both caches and repeat the request
        self.reactor.pump((10000.0,))

        # Repeat the request, this time it should fail if the lookup fails.
        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )

        clients = self.reactor.tcpClients
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        client_factory.clientConnectionFailed(None, Exception("nope"))
        self.reactor.pump((0.4,))

        r = self.successResultOf(fetch_d)
        self.assertEqual(r.delegated_server, None)

    def test_well_known_too_large(self) -> None:
        """A well-known query that returns a result which is too large should be rejected."""
        self.reactor.lookups["testserv"] = "1.2.3.4"

        fetch_d = defer.ensureDeferred(
            self.well_known_resolver.get_well_known(b"testserv")
        )

        # there should be an attempt to connect on port 443 for the .well-known
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 443)

        self._handle_well_known_connection(
            client_factory,
            expected_sni=b"testserv",
            response_headers={b"Cache-Control": b"max-age=1000"},
            content=b'{ "m.server": "' + (b"a" * WELL_KNOWN_MAX_SIZE) + b'" }',
        )

        # The result is successful, but disabled delegation.
        r = self.successResultOf(fetch_d)
        self.assertIsNone(r.delegated_server)

    def test_srv_fallbacks(self) -> None:
        """Test that other SRV results are tried if the first one fails for _matrix-fed SRV."""
        self.agent = self._make_agent()

        self.mock_resolver.resolve_service.return_value = [
            Server(host=b"target.com", port=8443),
            Server(host=b"target.com", port=8444),
        ]
        self.reactor.lookups["target.com"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix-fed._tcp.testserv"
        )

        # We should see an attempt to connect to the first server
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # Fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # There's a 300ms delay in HostnameEndpoint
        self.reactor.pump((0.4,))

        # Hasn't failed yet
        self.assertNoResult(test_d)

        # We shouldnow see an attempt to connect to the second server
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8444)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_srv_fallbacks_legacy(self) -> None:
        """Test that other SRV results are tried if the first one fails for _matrix SRV."""
        self.agent = self._make_agent()

        # Return no entries for the _matrix-fed lookup, and a response for _matrix.
        self.mock_resolver.resolve_service.side_effect = [
            [],
            [
                Server(host=b"target.com", port=8443),
                Server(host=b"target.com", port=8444),
            ],
        ]
        self.reactor.lookups["target.com"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        self.mock_resolver.resolve_service.assert_has_calls(
            [call(b"_matrix-fed._tcp.testserv"), call(b"_matrix._tcp.testserv")]
        )

        # We should see an attempt to connect to the first server
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # Fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # There's a 300ms delay in HostnameEndpoint
        self.reactor.pump((0.4,))

        # Hasn't failed yet
        self.assertNoResult(test_d)

        # We shouldnow see an attempt to connect to the second server
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8444)

        # make a test server, and wire up the client
        http_server = self._make_connection(client_factory, expected_sni=b"testserv")

        self.assertEqual(len(http_server.requests), 1)
        request = http_server.requests[0]
        self.assertEqual(request.method, b"GET")
        self.assertEqual(request.path, b"/foo/bar")
        self.assertEqual(request.requestHeaders.getRawHeaders(b"host"), [b"testserv"])

        # finish the request
        request.finish()
        self.reactor.pump((0.1,))
        self.successResultOf(test_d)

    def test_srv_no_fallback_to_legacy(self) -> None:
        """Test that _matrix SRV results are not tried if the _matrix-fed one fails."""
        self.agent = self._make_agent()

        # Return a failing entry for _matrix-fed.
        self.mock_resolver.resolve_service.side_effect = [
            [Server(host=b"target.com", port=8443)],
            [],
        ]
        self.reactor.lookups["target.com"] = "1.2.3.4"

        test_d = self._make_get_request(b"matrix-federation://testserv/foo/bar")

        # Nothing happened yet
        self.assertNoResult(test_d)

        # Only the _matrix-fed is checked, _matrix is ignored.
        self.mock_resolver.resolve_service.assert_called_once_with(
            b"_matrix-fed._tcp.testserv"
        )

        # We should see an attempt to connect to the first server
        clients = self.reactor.tcpClients
        self.assertEqual(len(clients), 1)
        (host, port, client_factory, _timeout, _bindAddress) = clients.pop(0)
        self.assertEqual(host, "1.2.3.4")
        self.assertEqual(port, 8443)

        # Fonx the connection
        client_factory.clientConnectionFailed(None, Exception("nope"))

        # There's a 300ms delay in HostnameEndpoint
        self.reactor.pump((0.4,))

        # Failed to resolve a server.
        self.assertFailure(test_d, Exception)


class TestCachePeriodFromHeaders(unittest.TestCase):
    def test_cache_control(self) -> None:
        # uppercase
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b"Cache-Control": [b"foo, Max-Age = 100, bar"]})
            ),
            100,
        )

        # missing value
        self.assertIsNone(
            _cache_period_from_headers(Headers({b"Cache-Control": [b"max-age=, bar"]}))
        )

        # hackernews: bogus due to semicolon
        self.assertIsNone(
            _cache_period_from_headers(
                Headers({b"Cache-Control": [b"private; max-age=0"]})
            )
        )

        # github
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b"Cache-Control": [b"max-age=0, private, must-revalidate"]})
            ),
            0,
        )

        # google
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b"cache-control": [b"private, max-age=0"]})
            ),
            0,
        )

    def test_expires(self) -> None:
        self.assertEqual(
            _cache_period_from_headers(
                Headers({b"Expires": [b"Wed, 30 Jan 2019 07:35:33 GMT"]}),
                time_now=lambda: 1548833700,
            ),
            33,
        )

        # cache-control overrides expires
        self.assertEqual(
            _cache_period_from_headers(
                Headers(
                    {
                        b"cache-control": [b"max-age=10"],
                        b"Expires": [b"Wed, 30 Jan 2019 07:35:33 GMT"],
                    }
                ),
                time_now=lambda: 1548833700,
            ),
            10,
        )

        # invalid expires means immediate expiry
        self.assertEqual(_cache_period_from_headers(Headers({b"Expires": [b"0"]})), 0)


def _check_logcontext(context: LoggingContextOrSentinel) -> None:
    current = current_context()
    if current is not context:
        raise AssertionError("Expected logcontext %s but was %s" % (context, current))


def _get_test_protocol_factory() -> IProtocolFactory:
    """Get a protocol Factory which will build an HTTPChannel
    Returns:
        interfaces.IProtocolFactory
    """
    server_factory = Factory.forProtocol(HTTPChannel)

    # Request.finish expects the factory to have a 'log' method.
    server_factory.log = _log_request

    return server_factory


def _log_request(request: str) -> None:
    """Implements Factory.log, which is expected by Request.finish"""
    logger.info("Completed request %s", request)


@implementer(IPolicyForHTTPS)
class TrustingTLSPolicyForHTTPS:
    """An IPolicyForHTTPS which checks that the certificate belongs to the
    right server, but doesn't check the certificate chain."""

    def creatorForNetloc(
        self, hostname: bytes, port: int
    ) -> IOpenSSLClientConnectionCreator:
        certificateOptions = OpenSSLCertificateOptions()
        return ClientTLSOptions(hostname, certificateOptions.getContext())
