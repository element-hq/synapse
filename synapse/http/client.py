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
import urllib.parse
from http import HTTPStatus
from io import BytesIO
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
    Union,
)

import attr
import multipart
import treq
from canonicaljson import encode_canonical_json
from netaddr import AddrFormatError, IPAddress, IPSet
from prometheus_client import Counter
from typing_extensions import Protocol
from zope.interface import implementer, provider

from OpenSSL import SSL
from OpenSSL.SSL import VERIFY_NONE
from twisted.internet import defer, error as twisted_error, protocol, ssl
from twisted.internet.address import IPv4Address, IPv6Address
from twisted.internet.interfaces import (
    IAddress,
    IDelayedCall,
    IHostResolution,
    IOpenSSLContextFactory,
    IReactorCore,
    IReactorPluggableNameResolver,
    IReactorTime,
    IResolutionReceiver,
    ITCPTransport,
)
from twisted.internet.protocol import connectionDone
from twisted.internet.task import Cooperator
from twisted.python.failure import Failure
from twisted.web._newclient import ResponseDone
from twisted.web.client import (
    Agent,
    HTTPConnectionPool,
    ResponseNeverReceived,
    readBody,
)
from twisted.web.http import PotentialDataLoss
from twisted.web.http_headers import Headers
from twisted.web.iweb import (
    UNKNOWN_LENGTH,
    IAgent,
    IBodyProducer,
    IPolicyForHTTPS,
    IResponse,
)

from synapse.api.errors import Codes, HttpResponseException, SynapseError
from synapse.http import QuieterFileBodyProducer, RequestTimedOutError, redact_uri
from synapse.http.proxyagent import ProxyAgent
from synapse.http.replicationagent import ReplicationAgent
from synapse.http.types import QueryParams
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.opentracing import set_tag, start_active_span, tags
from synapse.types import ISynapseReactor, StrSequence
from synapse.util import json_decoder
from synapse.util.async_helpers import timeout_deferred

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

outgoing_requests_counter = Counter("synapse_http_client_requests", "", ["method"])
incoming_responses_counter = Counter(
    "synapse_http_client_responses", "", ["method", "code"]
)

# the type of the headers map, to be passed to the t.w.h.Headers.
#
# The actual type accepted by Twisted is
#   Mapping[Union[str, bytes], Sequence[Union[str, bytes]] ,
# allowing us to mix and match str and bytes freely. However: any str is also a
# Sequence[str]; passing a header string value which is a
# standalone str is interpreted as a sequence of 1-codepoint strings. This is a disastrous footgun.
# We use a narrower value type (RawHeaderValue) to avoid this footgun.
#
# We also simplify the keys to be either all str or all bytes. This helps because
# Dict[K, V] is invariant in K (and indeed V).
RawHeaders = Union[Mapping[str, "RawHeaderValue"], Mapping[bytes, "RawHeaderValue"]]

# the value actually has to be a List, but List is invariant so we can't specify that
# the entries can either be Lists or bytes.
RawHeaderValue = Union[
    StrSequence,
    List[bytes],
    List[Union[str, bytes]],
    Tuple[bytes, ...],
    Tuple[Union[str, bytes], ...],
]


def _is_ip_blocked(
    ip_address: IPAddress, allowlist: Optional[IPSet], blocklist: IPSet
) -> bool:
    """
    Compares an IP address to allowed and disallowed IP sets.

    Args:
        ip_address: The IP address to check
        allowlist: Allowed IP addresses.
        blocklist: Disallowed IP addresses.

    Returns:
        True if the IP address is in the blocklist and not in the allowlist.
    """
    if ip_address in blocklist:
        if allowlist is None or ip_address not in allowlist:
            return True
    return False


_EPSILON = 0.00000001


def _make_scheduler(
    reactor: IReactorTime,
) -> Callable[[Callable[[], object]], IDelayedCall]:
    """Makes a schedular suitable for a Cooperator using the given reactor.

    (This is effectively just a copy from `twisted.internet.task`)
    """

    def _scheduler(x: Callable[[], object]) -> IDelayedCall:
        return reactor.callLater(_EPSILON, x)

    return _scheduler


class _IPBlockingResolver:
    """
    A proxy for reactor.nameResolver which only produces non-blocklisted IP
    addresses, preventing DNS rebinding attacks.
    """

    def __init__(
        self,
        reactor: IReactorPluggableNameResolver,
        ip_allowlist: Optional[IPSet],
        ip_blocklist: IPSet,
    ):
        """
        Args:
            reactor: The twisted reactor.
            ip_allowlist: IP addresses to allow.
            ip_blocklist: IP addresses to disallow.
        """
        self._reactor = reactor
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist

    def resolveHostName(
        self, recv: IResolutionReceiver, hostname: str, portNumber: int = 0
    ) -> IResolutionReceiver:
        addresses: List[IAddress] = []

        def _callback() -> None:
            has_bad_ip = False
            for address in addresses:
                # We only expect IPv4 and IPv6 addresses since only A/AAAA lookups
                # should go through this path.
                if not isinstance(address, (IPv4Address, IPv6Address)):
                    continue

                ip_address = IPAddress(address.host)

                if _is_ip_blocked(ip_address, self._ip_allowlist, self._ip_blocklist):
                    logger.info(
                        "Blocked %s from DNS resolution to %s" % (ip_address, hostname)
                    )
                    has_bad_ip = True

            # if we have a blocked IP, we'd like to raise an error to block the
            # request, but all we can really do from here is claim that there were no
            # valid results.
            if not has_bad_ip:
                for address in addresses:
                    recv.addressResolved(address)
            recv.resolutionComplete()

        @provider(IResolutionReceiver)
        class EndpointReceiver:
            @staticmethod
            def resolutionBegan(resolutionInProgress: IHostResolution) -> None:
                recv.resolutionBegan(resolutionInProgress)

            @staticmethod
            def addressResolved(address: IAddress) -> None:
                addresses.append(address)

            @staticmethod
            def resolutionComplete() -> None:
                _callback()

        self._reactor.nameResolver.resolveHostName(
            EndpointReceiver, hostname, portNumber=portNumber
        )

        return recv


# ISynapseReactor implies IReactorCore, but explicitly marking it this as an implementer
# of IReactorCore seems to keep mypy-zope happier.
@implementer(IReactorCore, ISynapseReactor)
class BlocklistingReactorWrapper:
    """
    A Reactor wrapper which will prevent DNS resolution to blocked IP
    addresses, to prevent DNS rebinding.
    """

    def __init__(
        self,
        reactor: IReactorPluggableNameResolver,
        ip_allowlist: Optional[IPSet],
        ip_blocklist: IPSet,
    ):
        self._reactor = reactor

        # We need to use a DNS resolver which filters out blocked IP
        # addresses, to prevent DNS rebinding.
        self._nameResolver = _IPBlockingResolver(
            self._reactor, ip_allowlist, ip_blocklist
        )

    def __getattr__(self, attr: str) -> Any:
        # Passthrough to the real reactor except for the DNS resolver.
        if attr == "nameResolver":
            return self._nameResolver
        else:
            return getattr(self._reactor, attr)


class BlocklistingAgentWrapper(Agent):
    """
    An Agent wrapper which will prevent access to IP addresses being accessed
    directly (without an IP address lookup).
    """

    def __init__(
        self,
        agent: IAgent,
        ip_blocklist: IPSet,
        ip_allowlist: Optional[IPSet] = None,
    ):
        """
        Args:
            agent: The Agent to wrap.
            ip_allowlist: IP addresses to allow.
            ip_blocklist: IP addresses to disallow.
        """
        self._agent = agent
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist

    def request(
        self,
        method: bytes,
        uri: bytes,
        headers: Optional[Headers] = None,
        bodyProducer: Optional[IBodyProducer] = None,
    ) -> defer.Deferred:
        h = urllib.parse.urlparse(uri.decode("ascii"))

        try:
            # h.hostname is Optional[str], None raises an AddrFormatError, so
            # this is safe even though IPAddress requires a str.
            ip_address = IPAddress(h.hostname)  # type: ignore[arg-type]
        except AddrFormatError:
            # Not an IP
            pass
        else:
            if _is_ip_blocked(ip_address, self._ip_allowlist, self._ip_blocklist):
                logger.info("Blocking access to %s" % (ip_address,))
                e = SynapseError(HTTPStatus.FORBIDDEN, "IP address blocked")
                return defer.fail(Failure(e))

        return self._agent.request(
            method, uri, headers=headers, bodyProducer=bodyProducer
        )


class BaseHttpClient:
    """
    A simple, no-frills HTTP client with methods that wrap up common ways of
    using HTTP in Matrix. Does not come with a default Agent, subclasses will need to
    define their own.

    Args:
        hs: The HomeServer instance to pass in
        treq_args: Extra keyword arguments to be given to treq.request.
    """

    agent: IAgent

    def __init__(
        self,
        hs: "HomeServer",
        treq_args: Optional[Dict[str, Any]] = None,
    ):
        self.hs = hs
        self.reactor = hs.get_reactor()

        self._extra_treq_args = treq_args or {}
        self.clock = hs.get_clock()

        user_agent = hs.version_string
        if hs.config.server.user_agent_suffix:
            user_agent = "%s %s" % (
                user_agent,
                hs.config.server.user_agent_suffix,
            )
        self.user_agent = user_agent.encode("ascii")

        # We use this for our body producers to ensure that they use the correct
        # reactor.
        self._cooperator = Cooperator(scheduler=_make_scheduler(hs.get_reactor()))

    async def request(
        self,
        method: str,
        uri: str,
        data: Optional[bytes] = None,
        headers: Optional[Headers] = None,
    ) -> IResponse:
        """
        Args:
            method: HTTP method to use.
            uri: URI to query.
            data: Data to send in the request body, if applicable.
            headers: Request headers.

        Returns:
            Response object, once the headers have been read.

        Raises:
            RequestTimedOutError if the request times out before the headers are read

        """
        outgoing_requests_counter.labels(method).inc()

        # log request but strip `access_token` (AS requests for example include this)
        logger.debug("Sending request %s %s", method, redact_uri(uri))

        with start_active_span(
            "outgoing-client-request",
            tags={
                tags.SPAN_KIND: tags.SPAN_KIND_RPC_CLIENT,
                tags.HTTP_METHOD: method,
                tags.HTTP_URL: uri,
            },
            finish_on_close=True,
        ):
            try:
                body_producer = None
                if data is not None:
                    body_producer = QuieterFileBodyProducer(
                        BytesIO(data),
                        cooperator=self._cooperator,
                    )

                # Always make sure we add a user agent to the request
                if headers is None:
                    headers = Headers()

                if not headers.hasHeader("User-Agent"):
                    headers.addRawHeader("User-Agent", self.user_agent)

                request_deferred: defer.Deferred = treq.request(
                    method,
                    uri,
                    agent=self.agent,
                    data=body_producer,
                    headers=headers,
                    # Avoid buffering the body in treq since we do not reuse
                    # response bodies.
                    unbuffered=True,
                    **self._extra_treq_args,
                )

                # we use our own timeout mechanism rather than treq's as a workaround
                # for https://twistedmatrix.com/trac/ticket/9534.
                request_deferred = timeout_deferred(
                    request_deferred,
                    60,
                    self.hs.get_reactor(),
                )

                # turn timeouts into RequestTimedOutErrors
                request_deferred.addErrback(_timeout_to_request_timed_out_error)

                response = await make_deferred_yieldable(request_deferred)

                incoming_responses_counter.labels(method, response.code).inc()
                logger.info(
                    "Received response to %s %s: %s",
                    method,
                    redact_uri(uri),
                    response.code,
                )
                return response
            except Exception as e:
                incoming_responses_counter.labels(method, "ERR").inc()
                logger.info(
                    "Error sending request to  %s %s: %s %s",
                    method,
                    redact_uri(uri),
                    type(e).__name__,
                    e.args[0],
                )
                set_tag(tags.ERROR, True)
                set_tag("error_reason", e.args[0])
                raise

    async def post_urlencoded_get_json(
        self,
        uri: str,
        args: Optional[Mapping[str, Union[str, List[str]]]] = None,
        headers: Optional[RawHeaders] = None,
    ) -> Any:
        """
        Args:
            uri: uri to query
            args: parameters to be url-encoded in the body
            headers: a map from header name to a list of values for that header

        Returns:
            parsed json

        Raises:
            RequestTimedOutError: if there is a timeout before the response headers
               are received. Note there is currently no timeout on reading the response
               body.

            HttpResponseException: On a non-2xx HTTP response.

            ValueError: if the response was not JSON
        """

        # TODO: Do we ever want to log message contents?
        logger.debug("post_urlencoded_get_json args: %s", args)

        query_bytes = encode_query_args(args)

        actual_headers = {
            b"Content-Type": [b"application/x-www-form-urlencoded"],
            b"User-Agent": [self.user_agent],
            b"Accept": [b"application/json"],
        }
        if headers:
            actual_headers.update(headers)  # type: ignore

        response = await self.request(
            "POST", uri, headers=Headers(actual_headers), data=query_bytes
        )

        body = await make_deferred_yieldable(readBody(response))

        if 200 <= response.code < 300:
            return json_decoder.decode(body.decode("utf-8"))
        else:
            raise HttpResponseException(
                response.code, response.phrase.decode("ascii", errors="replace"), body
            )

    async def post_json_get_json(
        self, uri: str, post_json: Any, headers: Optional[RawHeaders] = None
    ) -> Any:
        """

        Args:
            uri: URI to query.
            post_json: request body, to be encoded as json
            headers: a map from header name to a list of values for that header

        Returns:
            parsed json

        Raises:
            RequestTimedOutError: if there is a timeout before the response headers
               are received. Note there is currently no timeout on reading the response
               body.

            HttpResponseException: On a non-2xx HTTP response.

            ValueError: if the response was not JSON
        """
        json_str = encode_canonical_json(post_json)

        logger.debug("HTTP POST %s -> %s", json_str, uri)

        actual_headers = {
            b"Content-Type": [b"application/json"],
            b"User-Agent": [self.user_agent],
            b"Accept": [b"application/json"],
        }
        if headers:
            actual_headers.update(headers)  # type: ignore

        response = await self.request(
            "POST", uri, headers=Headers(actual_headers), data=json_str
        )

        body = await make_deferred_yieldable(readBody(response))

        if 200 <= response.code < 300:
            return json_decoder.decode(body.decode("utf-8"))
        else:
            raise HttpResponseException(
                response.code, response.phrase.decode("ascii", errors="replace"), body
            )

    async def get_json(
        self,
        uri: str,
        args: Optional[QueryParams] = None,
        headers: Optional[RawHeaders] = None,
    ) -> Any:
        """Gets some json from the given URI.

        Args:
            uri: The URI to request, not including query parameters
            args: A dictionary used to create query string
            headers: a map from header name to a list of values for that header
        Returns:
            Succeeds when we get a 2xx HTTP response, with the HTTP body as JSON.
        Raises:
            RequestTimedOutError: if there is a timeout before the response headers
               are received. Note there is currently no timeout on reading the response
               body.

            HttpResponseException On a non-2xx HTTP response.

            ValueError: if the response was not JSON
        """
        actual_headers = {b"Accept": [b"application/json"]}
        if headers:
            actual_headers.update(headers)  # type: ignore

        body = await self.get_raw(uri, args, headers=actual_headers)
        return json_decoder.decode(body.decode("utf-8"))

    async def put_json(
        self,
        uri: str,
        json_body: Any,
        args: Optional[QueryParams] = None,
        headers: Optional[RawHeaders] = None,
    ) -> Any:
        """Puts some json to the given URI.

        Args:
            uri: The URI to request, not including query parameters
            json_body: The JSON to put in the HTTP body,
            args: A dictionary used to create query strings
            headers: a map from header name to a list of values for that header
        Returns:
            Succeeds when we get a 2xx HTTP response, with the HTTP body as JSON.
        Raises:
             RequestTimedOutError: if there is a timeout before the response headers
               are received. Note there is currently no timeout on reading the response
               body.

            HttpResponseException On a non-2xx HTTP response.

            ValueError: if the response was not JSON
        """
        if args:
            query_str = urllib.parse.urlencode(args, True)
            uri = "%s?%s" % (uri, query_str)

        json_str = encode_canonical_json(json_body)

        actual_headers = {
            b"Content-Type": [b"application/json"],
            b"User-Agent": [self.user_agent],
            b"Accept": [b"application/json"],
        }
        if headers:
            actual_headers.update(headers)  # type: ignore

        response = await self.request(
            "PUT", uri, headers=Headers(actual_headers), data=json_str
        )

        body = await make_deferred_yieldable(readBody(response))

        if 200 <= response.code < 300:
            return json_decoder.decode(body.decode("utf-8"))
        else:
            raise HttpResponseException(
                response.code, response.phrase.decode("ascii", errors="replace"), body
            )

    async def get_raw(
        self,
        uri: str,
        args: Optional[QueryParams] = None,
        headers: Optional[RawHeaders] = None,
    ) -> bytes:
        """Gets raw text from the given URI.

        Args:
            uri: The URI to request, not including query parameters
            args: A dictionary used to create query strings
            headers: a map from header name to a list of values for that header
        Returns:
            Succeeds when we get a 2xx HTTP response, with the
            HTTP body as bytes.
        Raises:
            RequestTimedOutError: if there is a timeout before the response headers
               are received. Note there is currently no timeout on reading the response
               body.

            HttpResponseException on a non-2xx HTTP response.
        """
        if args:
            query_str = urllib.parse.urlencode(args, True)
            uri = "%s?%s" % (uri, query_str)

        actual_headers = {b"User-Agent": [self.user_agent]}
        if headers:
            actual_headers.update(headers)  # type: ignore

        response = await self.request("GET", uri, headers=Headers(actual_headers))

        body = await make_deferred_yieldable(readBody(response))

        if 200 <= response.code < 300:
            return body
        else:
            raise HttpResponseException(
                response.code, response.phrase.decode("ascii", errors="replace"), body
            )

    # XXX: FIXME: This is horribly copy-pasted from matrixfederationclient.
    # The two should be factored out.

    async def get_file(
        self,
        url: str,
        output_stream: BinaryIO,
        max_size: Optional[int] = None,
        headers: Optional[RawHeaders] = None,
        is_allowed_content_type: Optional[Callable[[str], bool]] = None,
    ) -> Tuple[int, Dict[bytes, List[bytes]], str, int]:
        """GETs a file from a given URL
        Args:
            url: The URL to GET
            output_stream: File to write the response body to.
            headers: A map from header name to a list of values for that header
            is_allowed_content_type: A predicate to determine whether the
                content type of the file we're downloading is allowed. If set and
                it evaluates to False when called with the content type, the
                request will be terminated before completing the download by
                raising SynapseError.
        Returns:
            A tuple of the file length, dict of the response
            headers, absolute URI of the response and HTTP response code.

        Raises:
            RequestTimedOutError: if there is a timeout before the response headers
               are received. Note there is currently no timeout on reading the response
               body.

            SynapseError: if the response is not a 2xx, the remote file is too large, or
               another exception happens during the download.
        """

        actual_headers = {b"User-Agent": [self.user_agent]}
        if headers:
            actual_headers.update(headers)  # type: ignore

        response = await self.request("GET", url, headers=Headers(actual_headers))

        resp_headers = dict(response.headers.getAllRawHeaders())

        if response.code > 299:
            logger.warning("Got %d when downloading %s" % (response.code, url))
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY, "Got error %d" % (response.code,), Codes.UNKNOWN
            )

        if is_allowed_content_type and b"Content-Type" in resp_headers:
            content_type = resp_headers[b"Content-Type"][0].decode("ascii")
            if not is_allowed_content_type(content_type):
                raise SynapseError(
                    HTTPStatus.BAD_GATEWAY,
                    (
                        "Requested file's content type not allowed for this operation: %s"
                        % content_type
                    ),
                )

        # TODO: if our Content-Type is HTML or something, just read the first
        # N bytes into RAM rather than saving it all to disk only to read it
        # straight back in again

        try:
            d = read_body_with_max_size(response, output_stream, max_size)

            # Ensure that the body is not read forever.
            d = timeout_deferred(d, 30, self.hs.get_reactor())

            length = await make_deferred_yieldable(d)
        except BodyExceededMaxSize:
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                "Requested file is too large > %r bytes" % (max_size,),
                Codes.TOO_LARGE,
            )
        except defer.TimeoutError:
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                "Requested file took too long to download",
                Codes.TOO_LARGE,
            )
        except Exception as e:
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY, ("Failed to download remote body: %s" % e)
            ) from e

        return (
            length,
            resp_headers,
            response.request.absoluteURI.decode("ascii"),
            response.code,
        )


class SimpleHttpClient(BaseHttpClient):
    """
    An HTTP client capable of crossing a proxy and respecting a block/allow list.

    This also configures a larger / longer lasting HTTP connection pool.

    Args:
        hs: The HomeServer instance to pass in
        treq_args: Extra keyword arguments to be given to treq.request.
        ip_blocklist: The IP addresses that we may not request.
        ip_allowlist: The allowed IP addresses, that we can
           request if it were otherwise caught in a blocklist.
        use_proxy: Whether proxy settings should be discovered and used
            from conventional environment variables.
    """

    def __init__(
        self,
        hs: "HomeServer",
        treq_args: Optional[Dict[str, Any]] = None,
        ip_allowlist: Optional[IPSet] = None,
        ip_blocklist: Optional[IPSet] = None,
        use_proxy: bool = False,
    ):
        super().__init__(hs, treq_args=treq_args)
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist

        if self._ip_blocklist:
            # If we have an IP blocklist, we need to use a DNS resolver which
            # filters out blocked IP addresses, to prevent DNS rebinding.
            self.reactor: ISynapseReactor = BlocklistingReactorWrapper(
                self.reactor, self._ip_allowlist, self._ip_blocklist
            )

        # the pusher makes lots of concurrent SSL connections to Sygnal, and tends to
        # do so in batches, so we need to allow the pool to keep lots of idle
        # connections around.
        pool = HTTPConnectionPool(self.reactor)
        # XXX: The justification for using the cache factor here is that larger
        # instances will need both more cache and more connections.
        # Still, this should probably be a separate dial
        pool.maxPersistentPerHost = max(int(100 * hs.config.caches.global_factor), 5)
        pool.cachedConnectionTimeout = 2 * 60

        self.agent: IAgent = ProxyAgent(
            self.reactor,
            hs.get_reactor(),
            connectTimeout=15,
            contextFactory=self.hs.get_http_client_context_factory(),
            pool=pool,
            use_proxy=use_proxy,
        )

        if self._ip_blocklist:
            # If we have an IP blocklist, we then install the Agent which prevents
            # direct access to IP addresses, that are not caught by the DNS resolution.
            self.agent = BlocklistingAgentWrapper(
                self.agent,
                ip_blocklist=self._ip_blocklist,
                ip_allowlist=self._ip_allowlist,
            )


class ReplicationClient(BaseHttpClient):
    """Client for connecting to replication endpoints via HTTP and HTTPS.

    Attributes:
        agent: The custom Twisted Agent used for constructing the connection.
    """

    def __init__(
        self,
        hs: "HomeServer",
    ):
        """
        Args:
            hs: The HomeServer instance to pass in
        """
        super().__init__(hs)

        # Use a pool, but a very small one.
        pool = HTTPConnectionPool(self.reactor)
        pool.maxPersistentPerHost = 5
        pool.cachedConnectionTimeout = 2 * 60

        self.agent: IAgent = ReplicationAgent(
            hs.get_reactor(),
            hs.config.worker.instance_map,
            contextFactory=hs.get_http_client_context_factory(),
            pool=pool,
        )

    async def request(
        self,
        method: str,
        uri: str,
        data: Optional[bytes] = None,
        headers: Optional[Headers] = None,
    ) -> IResponse:
        """
        Make a request, differs from BaseHttpClient.request in that it does not use treq.

        Args:
            method: HTTP method to use.
            uri: URI to query.
            data: Data to send in the request body, if applicable.
            headers: Request headers.

        Returns:
            Response object, once the headers have been read.

        Raises:
            RequestTimedOutError if the request times out before the headers are read

        """
        outgoing_requests_counter.labels(method).inc()

        logger.debug("Sending request %s %s", method, uri)

        with start_active_span(
            "outgoing-replication-request",
            tags={
                tags.SPAN_KIND: tags.SPAN_KIND_RPC_CLIENT,
                tags.HTTP_METHOD: method,
                tags.HTTP_URL: uri,
            },
            finish_on_close=True,
        ):
            try:
                body_producer = None
                if data is not None:
                    body_producer = QuieterFileBodyProducer(
                        BytesIO(data),
                        cooperator=self._cooperator,
                    )

                # Skip the fancy treq stuff, we don't need cookie handling, redirects,
                # or buffered response bodies.
                method_bytes = method.encode("ascii")
                uri_bytes = uri.encode("ascii")

                # To preserve the logging context, the timeout is treated
                # in a similar way to `defer.gatherResults`:
                # * Each logging context-preserving fork is wrapped in
                #   `run_in_background`. In this case there is only one,
                #   since the timeout fork is not logging-context aware.
                # * The `Deferred` that joins the forks back together is
                #   wrapped in `make_deferred_yieldable` to restore the
                #   logging context regardless of the path taken.
                # (The logic/comments for this came from MatrixFederationHttpClient)
                request_deferred = run_in_background(
                    self.agent.request,
                    method_bytes,
                    uri_bytes,
                    headers,
                    bodyProducer=body_producer,
                )

                # we use our own timeout mechanism rather than twisted's as a workaround
                # for https://twistedmatrix.com/trac/ticket/9534.
                # (Updated url https://github.com/twisted/twisted/issues/9534)
                request_deferred = timeout_deferred(
                    request_deferred,
                    60,
                    self.hs.get_reactor(),
                )

                # turn timeouts into RequestTimedOutErrors
                request_deferred.addErrback(_timeout_to_request_timed_out_error)

                response = await make_deferred_yieldable(request_deferred)

                incoming_responses_counter.labels(method, response.code).inc()
                logger.info(
                    "Received response to %s %s: %s",
                    method,
                    uri,
                    response.code,
                )
                return response
            except Exception as e:
                incoming_responses_counter.labels(method, "ERR").inc()
                logger.info(
                    "Error sending request to  %s %s: %s %s",
                    method,
                    uri,
                    type(e).__name__,
                    e.args[0],
                )
                set_tag(tags.ERROR, True)
                set_tag("error_reason", e.args[0])
                raise


def _timeout_to_request_timed_out_error(f: Failure) -> Failure:
    if f.check(twisted_error.TimeoutError, twisted_error.ConnectingCancelledError):
        # The TCP connection has its own timeout (set by the 'connectTimeout' param
        # on the Agent), which raises twisted_error.TimeoutError exception.
        raise RequestTimedOutError("Timeout connecting to remote server")
    elif f.check(defer.TimeoutError, ResponseNeverReceived):
        # this one means that we hit our overall timeout on the request
        raise RequestTimedOutError("Timeout waiting for response from remote server")

    return f


class ByteWriteable(Protocol):
    """The type of object which must be passed into read_body_with_max_size.

    Typically this is a file object.
    """

    def write(self, data: bytes) -> int:
        pass


class BodyExceededMaxSize(Exception):
    """The maximum allowed size of the HTTP body was exceeded."""


class _DiscardBodyWithMaxSizeProtocol(protocol.Protocol):
    """A protocol which immediately errors upon receiving data."""

    transport: Optional[ITCPTransport] = None

    def __init__(self, deferred: defer.Deferred):
        self.deferred = deferred

    def _maybe_fail(self) -> None:
        """
        Report a max size exceed error and disconnect the first time this is called.
        """
        if not self.deferred.called:
            self.deferred.errback(BodyExceededMaxSize())
            # Close the connection (forcefully) since all the data will get
            # discarded anyway.
            assert self.transport is not None
            self.transport.abortConnection()

    def dataReceived(self, data: bytes) -> None:
        self._maybe_fail()

    def connectionLost(self, reason: Failure = connectionDone) -> None:
        self._maybe_fail()


@attr.s(auto_attribs=True, slots=True)
class MultipartResponse:
    """
    A small class to hold parsed values of a multipart response.
    """

    json: bytes = b"{}"
    length: Optional[int] = None
    content_type: Optional[bytes] = None
    disposition: Optional[bytes] = None
    url: Optional[bytes] = None


class _MultipartParserProtocol(protocol.Protocol):
    """
    Protocol to read and parse a MSC3916 multipart/mixed response
    """

    transport: Optional[ITCPTransport] = None

    def __init__(
        self,
        stream: ByteWriteable,
        deferred: defer.Deferred,
        boundary: str,
        max_length: Optional[int],
    ) -> None:
        self.stream = stream
        self.deferred = deferred
        self.boundary = boundary
        self.max_length = max_length
        self.parser = None
        self.multipart_response = MultipartResponse()
        self.has_redirect = False
        self.in_json = False
        self.json_done = False
        self.file_length = 0
        self.total_length = 0
        self.in_disposition = False
        self.in_content_type = False

    def dataReceived(self, incoming_data: bytes) -> None:
        if self.deferred.called:
            return

        # we don't have a parser yet, instantiate it
        if not self.parser:

            def on_header_field(data: bytes, start: int, end: int) -> None:
                if data[start:end].lower() == b"location":
                    self.has_redirect = True
                if data[start:end].lower() == b"content-disposition":
                    self.in_disposition = True
                if data[start:end].lower() == b"content-type":
                    self.in_content_type = True

            def on_header_value(data: bytes, start: int, end: int) -> None:
                # the first header should be content-type for application/json
                if not self.in_json and not self.json_done:
                    assert data[start:end] == b"application/json"
                    self.in_json = True
                elif self.has_redirect:
                    self.multipart_response.url = data[start:end]
                elif self.in_content_type:
                    self.multipart_response.content_type = data[start:end]
                    self.in_content_type = False
                elif self.in_disposition:
                    self.multipart_response.disposition = data[start:end]
                    self.in_disposition = False

            def on_part_data(data: bytes, start: int, end: int) -> None:
                # we've seen json header but haven't written the json data
                if self.in_json and not self.json_done:
                    self.multipart_response.json = data[start:end]
                    self.json_done = True
                # we have a redirect header rather than a file, and have already captured it
                elif self.has_redirect:
                    return
                # otherwise we are in the file part
                else:
                    try:
                        self.stream.write(data[start:end])
                    except Exception as e:
                        logger.warning(
                            f"Exception encountered writing file data to stream: {e}"
                        )
                        self.deferred.errback()
                    self.file_length += end - start

            callbacks = {
                "on_header_field": on_header_field,
                "on_header_value": on_header_value,
                "on_part_data": on_part_data,
            }
            self.parser = multipart.MultipartParser(self.boundary, callbacks)

        self.total_length += len(incoming_data)
        if self.max_length is not None and self.total_length >= self.max_length:
            self.deferred.errback(BodyExceededMaxSize())
            # Close the connection (forcefully) since all the data will get
            # discarded anyway.
            assert self.transport is not None
            self.transport.abortConnection()

        try:
            self.parser.write(incoming_data)  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(f"Exception writing to multipart parser: {e}")
            self.deferred.errback()
            return

    def connectionLost(self, reason: Failure = connectionDone) -> None:
        # If the maximum size was already exceeded, there's nothing to do.
        if self.deferred.called:
            return

        if reason.check(ResponseDone):
            self.multipart_response.length = self.file_length
            self.deferred.callback(self.multipart_response)
        else:
            self.deferred.errback(reason)


class _ReadBodyWithMaxSizeProtocol(protocol.Protocol):
    """A protocol which reads body to a stream, erroring if the body exceeds a maximum size."""

    transport: Optional[ITCPTransport] = None

    def __init__(
        self, stream: ByteWriteable, deferred: defer.Deferred, max_size: Optional[int]
    ):
        self.stream = stream
        self.deferred = deferred
        self.length = 0
        self.max_size = max_size

    def dataReceived(self, data: bytes) -> None:
        # If the deferred was called, bail early.
        if self.deferred.called:
            return

        try:
            self.stream.write(data)
        except Exception:
            self.deferred.errback()
            return

        self.length += len(data)
        # The first time the maximum size is exceeded, error and cancel the
        # connection. dataReceived might be called again if data was received
        # in the meantime.
        if self.max_size is not None and self.length >= self.max_size:
            self.deferred.errback(BodyExceededMaxSize())
            # Close the connection (forcefully) since all the data will get
            # discarded anyway.
            assert self.transport is not None
            self.transport.abortConnection()

    def connectionLost(self, reason: Failure = connectionDone) -> None:
        # If the maximum size was already exceeded, there's nothing to do.
        if self.deferred.called:
            return

        if reason.check(ResponseDone):
            self.deferred.callback(self.length)
        elif reason.check(PotentialDataLoss):
            # This applies to requests which don't set `Content-Length` or a
            # `Transfer-Encoding` in the response because in this case the end of the
            # response is indicated by the connection being closed, an event which may
            # also be due to a transient network problem or other error. But since this
            # behavior is expected of some servers (like YouTube), let's ignore it.
            # Stolen from https://github.com/twisted/treq/pull/49/files
            # http://twistedmatrix.com/trac/ticket/4840
            self.deferred.callback(self.length)
        else:
            self.deferred.errback(reason)


def read_body_with_max_size(
    response: IResponse, stream: ByteWriteable, max_size: Optional[int]
) -> "defer.Deferred[int]":
    """
    Read a HTTP response body to a file-object. Optionally enforcing a maximum file size.

    If the maximum file size is reached, the returned Deferred will resolve to a
    Failure with a BodyExceededMaxSize exception.

    Args:
        response: The HTTP response to read from.
        stream: The file-object to write to.
        max_size: The maximum file size to allow.

    Returns:
        A Deferred which resolves to the length of the read body.
    """
    d: "defer.Deferred[int]" = defer.Deferred()

    # If the Content-Length header gives a size larger than the maximum allowed
    # size, do not bother downloading the body.
    if max_size is not None and response.length != UNKNOWN_LENGTH:
        if response.length > max_size:
            response.deliverBody(_DiscardBodyWithMaxSizeProtocol(d))
            return d

    response.deliverBody(_ReadBodyWithMaxSizeProtocol(stream, d, max_size))
    return d


def read_multipart_response(
    response: IResponse, stream: ByteWriteable, boundary: str, max_length: Optional[int]
) -> "defer.Deferred[MultipartResponse]":
    """
    Reads a MSC3916 multipart/mixed response and parses it, reading the file part (if it contains one) into
    the stream passed in and returning a deferred resolving to a MultipartResponse

    Args:
        response: The HTTP response to read from.
        stream: The file-object to write to.
        boundary: the multipart/mixed boundary string
        max_length: maximum allowable length of the response
    """
    d: defer.Deferred[MultipartResponse] = defer.Deferred()

    # If the Content-Length header gives a size larger than the maximum allowed
    # size, do not bother downloading the body.
    if max_length is not None and response.length != UNKNOWN_LENGTH:
        if response.length > max_length:
            response.deliverBody(_DiscardBodyWithMaxSizeProtocol(d))
            return d

    response.deliverBody(_MultipartParserProtocol(stream, d, boundary, max_length))
    return d


def encode_query_args(args: Optional[QueryParams]) -> bytes:
    """
    Encodes a map of query arguments to bytes which can be appended to a URL.

    Args:
        args: The query arguments, a mapping of string to string or list of strings.

    Returns:
        The query arguments encoded as bytes.
    """
    if args is None:
        return b""

    query_str = urllib.parse.urlencode(args, True)

    return query_str.encode("utf8")


@implementer(IPolicyForHTTPS)
class InsecureInterceptableContextFactory(ssl.ContextFactory):
    """
    Factory for PyOpenSSL SSL contexts which accepts any certificate for any domain.

    Do not use this since it allows an attacker to intercept your communications.
    """

    def __init__(self) -> None:
        self._context = SSL.Context(SSL.SSLv23_METHOD)
        self._context.set_verify(VERIFY_NONE, lambda *_: False)

    def getContext(self) -> SSL.Context:
        return self._context

    def creatorForNetloc(self, hostname: bytes, port: int) -> IOpenSSLContextFactory:
        return self


def is_unknown_endpoint(
    e: HttpResponseException, synapse_error: Optional[SynapseError] = None
) -> bool:
    """
    Returns true if the response was due to an endpoint being unimplemented.

    Args:
        e: The error response received from the remote server.
        synapse_error: The above error converted to a SynapseError. This is
            automatically generated if not provided.

    """
    if synapse_error is None:
        synapse_error = e.to_synapse_error()

    # Matrix v1.6 specifies that servers should return a 404 or 405 with an errcode
    # of M_UNRECOGNIZED when they receive a request to an unknown endpoint or
    # to an unknown method, respectively.
    #
    # Older versions of servers don't return proper errors, so be graceful. But,
    # also handle that some endpoints truly do return 404 errors.
    return (
        # 404 is an unknown endpoint, 405 is a known endpoint, but unknown method.
        (e.code == 404 or e.code == 405)
        and (
            # Consider empty body or non-JSON bodies to be unrecognised (matches
            # older Dendrites & Conduits).
            not e.response
            or not e.response.startswith(b"{")
            # The proper response JSON with M_UNRECOGNIZED errcode.
            or synapse_error.errcode == Codes.UNRECOGNIZED
        )
    ) or (
        # Older Synapses returned a 400 error.
        e.code == 400 and synapse_error.errcode == Codes.UNRECOGNIZED
    )
