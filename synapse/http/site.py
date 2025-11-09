#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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
import contextlib
import logging
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Generator

import attr
from zope.interface import implementer

from twisted.internet.address import UNIXAddress
from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IAddress
from twisted.internet.protocol import Protocol
from twisted.python.failure import Failure
from twisted.web.http import HTTPChannel
from twisted.web.resource import IResource, Resource
from twisted.web.server import Request

from synapse.config.server import ListenerConfig
from synapse.http import get_request_user_agent, redact_uri
from synapse.http.proxy import ProxySite
from synapse.http.request_metrics import RequestMetrics, requests_counter
from synapse.logging.context import (
    ContextRequest,
    LoggingContext,
    PreserveLoggingContext,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.types import ISynapseReactor, Requester

if TYPE_CHECKING:
    import opentracing

    from synapse.server import HomeServer


logger = logging.getLogger(__name__)

_next_request_seq = 0


class SynapseRequest(Request):
    """Class which encapsulates an HTTP request to synapse.

    All of the requests processed in synapse are of this type.

    It extends twisted's twisted.web.server.Request, and adds:
     * Unique request ID
     * A log context associated with the request
     * Redaction of access_token query-params in __repr__
     * Logging at start and end
     * Metrics to record CPU, wallclock and DB time by endpoint.
     * A limit to the size of request which will be accepted

    It also provides a method `processing`, which returns a context manager. If this
    method is called, the request won't be logged until the context manager is closed;
    this is useful for asynchronous request handlers which may go on processing the
    request even after the client has disconnected.

    Attributes:
        logcontext: the log context for this request
    """

    def __init__(
        self,
        channel: HTTPChannel,
        site: "SynapseSite",
        our_server_name: str,
        *args: Any,
        max_request_body_size: int = 1024,
        request_id_header: str | None = None,
        **kw: Any,
    ):
        super().__init__(channel, *args, **kw)
        self.our_server_name = our_server_name
        self._max_request_body_size = max_request_body_size
        self.request_id_header = request_id_header
        self.synapse_site = site
        self.reactor = site.reactor
        self._channel = channel  # this is used by the tests
        self.start_time = 0.0

        # The requester, if authenticated. For federation requests this is the
        # server name, for client requests this is the Requester object.
        self._requester: Requester | str | None = None

        # An opentracing span for this request. Will be closed when the request is
        # completely processed.
        self._opentracing_span: "opentracing.Span | None" = None

        # we can't yet create the logcontext, as we don't know the method.
        self.logcontext: LoggingContext | None = None

        # The `Deferred` to cancel if the client disconnects early and
        # `is_render_cancellable` is set. Expected to be set by `Resource.render`.
        self.render_deferred: "Deferred[None]" | None = None
        # A boolean indicating whether `render_deferred` should be cancelled if the
        # client disconnects early. Expected to be set by the coroutine started by
        # `Resource.render`, if rendering is asynchronous.
        self.is_render_cancellable: bool = False

        global _next_request_seq
        self.request_seq = _next_request_seq
        _next_request_seq += 1

        # whether an asynchronous request handler has called processing()
        self._is_processing = False

        # the time when the asynchronous request handler completed its processing
        self._processing_finished_time: float | None = None

        # what time we finished sending the response to the client (or the connection
        # dropped)
        self.finish_time: float | None = None

    def __repr__(self) -> str:
        # We overwrite this so that we don't log ``access_token``
        return "<%s at 0x%x method=%r uri=%r clientproto=%r site=%r>" % (
            self.__class__.__name__,
            id(self),
            self.get_method(),
            self.get_redacted_uri(),
            self.clientproto.decode("ascii", errors="replace"),
            self.synapse_site.site_tag,
        )

    # Twisted machinery: this method is called by the Channel once the full request has
    # been received, to dispatch the request to a resource.
    #
    # We're patching Twisted to bail/abort early when we see someone trying to upload
    # `multipart/form-data` so we can avoid Twisted parsing the entire request body into
    # in-memory (specific problem of this specific `Content-Type`). This protects us
    # from an attacker uploading something bigger than the available RAM and crashing
    # the server with a `MemoryError`, or carefully block just enough resources to cause
    # all other requests to fail.
    #
    # FIXME: This can be removed once we Twisted releases a fix and we update to a
    # version that is patched
    def requestReceived(self, command: bytes, path: bytes, version: bytes) -> None:
        if command == b"POST":
            ctype = self.requestHeaders.getRawHeaders(b"content-type")
            if ctype and b"multipart/form-data" in ctype[0]:
                self.method, self.uri = command, path
                self.clientproto = version
                self.code = HTTPStatus.UNSUPPORTED_MEDIA_TYPE.value
                self.code_message = bytes(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE.phrase, "ascii"
                )
                self.responseHeaders.setRawHeaders(b"content-length", [b"0"])

                logger.warning(
                    "Aborting connection from %s because `content-type: multipart/form-data` is unsupported: %s %s",
                    self.client,
                    command,
                    path,
                )
                self.write(b"")
                self.loseConnection()
                return
        return super().requestReceived(command, path, version)

    def handleContentChunk(self, data: bytes) -> None:
        # we should have a `content` by now.
        assert self.content, "handleContentChunk() called before gotLength()"
        if self.content.tell() + len(data) > self._max_request_body_size:
            logger.warning(
                "Aborting connection from %s because the request exceeds maximum size: %s %s",
                self.client,
                self.get_method(),
                self.get_redacted_uri(),
            )
            if self.channel:
                self.channel.forceAbortClient()
            return
        super().handleContentChunk(data)

    @property
    def requester(self) -> Requester | str | None:
        return self._requester

    @requester.setter
    def requester(self, value: Requester | str) -> None:
        # Store the requester, and update some properties based on it.

        # This should only be called once.
        assert self._requester is None

        self._requester = value

        # A logging context should exist by now (and have a ContextRequest).
        assert self.logcontext is not None
        assert self.logcontext.request is not None

        (
            requester,
            authenticated_entity,
        ) = self.get_authenticated_entity()
        self.logcontext.request.requester = requester
        # If there's no authenticated entity, it was the requester.
        self.logcontext.request.authenticated_entity = authenticated_entity or requester

    def set_opentracing_span(self, span: "opentracing.Span") -> None:
        """attach an opentracing span to this request

        Doing so will cause the span to be closed when we finish processing the request
        """
        self._opentracing_span = span

    def get_request_id(self) -> str:
        request_id_value = None
        if self.request_id_header:
            request_id_value = self.getHeader(self.request_id_header)

        if request_id_value is None:
            request_id_value = str(self.request_seq)

        return "%s-%s" % (self.get_method(), request_id_value)

    def get_redacted_uri(self) -> str:
        """Gets the redacted URI associated with the request (or placeholder if the URI
        has not yet been received).

        Note: This is necessary as the placeholder value in twisted is str
        rather than bytes, so we need to sanitise `self.uri`.

        Returns:
            The redacted URI as a string.
        """
        uri: bytes | str = self.uri
        if isinstance(uri, bytes):
            uri = uri.decode("ascii", errors="replace")
        return redact_uri(uri)

    def get_method(self) -> str:
        """Gets the method associated with the request (or placeholder if method
        has not yet been received).

        Note: This is necessary as the placeholder value in twisted is str
        rather than bytes, so we need to sanitise `self.method`.

        Returns:
            The request method as a string.
        """
        method: bytes | str = self.method
        if isinstance(method, bytes):
            return self.method.decode("ascii")
        return method

    def get_authenticated_entity(self) -> tuple[str | None, str | None]:
        """
        Get the "authenticated" entity of the request, which might be the user
        performing the action, or a user being puppeted by a server admin.

        Returns:
            A tuple:
                The first item is a string representing the user making the request.

                The second item is a string or None representing the user who
                authenticated when making this request. See
                Requester.authenticated_entity.
        """
        # Convert the requester into a string that we can log
        if isinstance(self._requester, str):
            return self._requester, None
        elif isinstance(self._requester, Requester):
            requester = self._requester.user.to_string()
            authenticated_entity = self._requester.authenticated_entity

            # If this is a request where the target user doesn't match the user who
            # authenticated (e.g. and admin is puppetting a user) then we return both.
            if requester != authenticated_entity:
                return requester, authenticated_entity

            return requester, None
        elif self._requester is not None:
            # This shouldn't happen, but we log it so we don't lose information
            # and can see that we're doing something wrong.
            return repr(self._requester), None  # type: ignore[unreachable]

        return None, None

    def render(self, resrc: Resource) -> None:
        # this is called once a Resource has been found to serve the request; in our
        # case the Resource in question will normally be a JsonResource.

        # Create a LogContext for this request
        #
        # We only care about associating logs and tallying up metrics at the per-request
        # level so we don't worry about setting the `parent_context`; preventing us from
        # unnecessarily piling up metrics on the main process's context.
        request_id = self.get_request_id()
        self.logcontext = LoggingContext(
            name=request_id,
            server_name=self.our_server_name,
            request=ContextRequest(
                request_id=request_id,
                ip_address=self.get_client_ip_if_available(),
                site_tag=self.synapse_site.site_tag,
                # The requester is going to be unknown at this point.
                requester=None,
                authenticated_entity=None,
                method=self.get_method(),
                url=self.get_redacted_uri(),
                protocol=self.clientproto.decode("ascii", errors="replace"),
                user_agent=get_request_user_agent(self),
            ),
        )

        # override the Server header which is set by twisted
        self.setHeader("Server", self.synapse_site.server_version_string)

        with PreserveLoggingContext(self.logcontext):
            # we start the request metrics timer here with an initial stab
            # at the servlet name. For most requests that name will be
            # JsonResource (or a subclass), and JsonResource._async_render
            # will update it once it picks a servlet.
            servlet_name = resrc.__class__.__name__
            self._started_processing(servlet_name)

            Request.render(self, resrc)

            # record the arrival of the request *after*
            # dispatching to the handler, so that the handler
            # can update the servlet name in the request
            # metrics
            requests_counter.labels(
                method=self.get_method(),
                servlet=self.request_metrics.name,
                **{SERVER_NAME_LABEL: self.our_server_name},
            ).inc()

    @contextlib.contextmanager
    def processing(self) -> Generator[None, None, None]:
        """Record the fact that we are processing this request.

        Returns a context manager; the correct way to use this is:

        async def handle_request(request):
            with request.processing("FooServlet"):
                await really_handle_the_request()

        Once the context manager is closed, the completion of the request will be logged,
        and the various metrics will be updated.
        """
        if self._is_processing:
            raise RuntimeError("Request is already processing")
        self._is_processing = True

        try:
            yield
        except Exception:
            # this should already have been caught, and sent back to the client as a 500.
            logger.exception(
                "Asynchronous message handler raised an uncaught exception"
            )
        finally:
            # the request handler has finished its work and either sent the whole response
            # back, or handed over responsibility to a Producer.

            self._processing_finished_time = time.time()
            self._is_processing = False

            if self._opentracing_span:
                self._opentracing_span.log_kv({"event": "finished processing"})

            # if we've already sent the response, log it now; otherwise, we wait for the
            # response to be sent.
            if self.finish_time is not None:
                self._finished_processing()

    def finish(self) -> None:
        """Called when all response data has been written to this Request.

        Overrides twisted.web.server.Request.finish to record the finish time and do
        logging.
        """
        self.finish_time = time.time()
        Request.finish(self)
        if self._opentracing_span:
            self._opentracing_span.log_kv({"event": "response sent"})
        if not self._is_processing:
            assert self.logcontext is not None
            with PreserveLoggingContext(self.logcontext):
                self._finished_processing()

    def connectionLost(self, reason: Failure | Exception) -> None:
        """Called when the client connection is closed before the response is written.

        Overrides twisted.web.server.Request.connectionLost to record the finish time and
        do logging.
        """
        # There is a bug in Twisted where reason is not wrapped in a Failure object
        # Detect this and wrap it manually as a workaround
        # More information: https://github.com/matrix-org/synapse/issues/7441
        if not isinstance(reason, Failure):
            reason = Failure(reason)

        self.finish_time = time.time()
        Request.connectionLost(self, reason)

        if self.logcontext is None:
            logger.info(
                "Connection from %s lost before request headers were read", self.client
            )
            return

        # we only get here if the connection to the client drops before we send
        # the response.
        #
        # It's useful to log it here so that we can get an idea of when
        # the client disconnects.
        with PreserveLoggingContext(self.logcontext):
            logger.info("Connection from client lost before response was sent")

            if self._opentracing_span:
                self._opentracing_span.log_kv(
                    {"event": "client connection lost", "reason": str(reason.value)}
                )

            if self._is_processing:
                if self.is_render_cancellable:
                    if self.render_deferred is not None:
                        # Throw a cancellation into the request processing, in the hope
                        # that it will finish up sooner than it normally would.
                        # The `self.processing()` context manager will call
                        # `_finished_processing()` when done.
                        with PreserveLoggingContext():
                            self.render_deferred.cancel()
                    else:
                        logger.error(
                            "Connection from client lost, but have no Deferred to "
                            "cancel even though the request is marked as cancellable."
                        )
            else:
                self._finished_processing()

    def _started_processing(self, servlet_name: str) -> None:
        """Record the fact that we are processing this request.

        This will log the request's arrival. Once the request completes,
        be sure to call finished_processing.

        Args:
            servlet_name: the name of the servlet which will be
                processing this request. This is used in the metrics.

                It is possible to update this afterwards by updating
                self.request_metrics.name.
        """
        self.start_time = time.time()
        self.request_metrics = RequestMetrics(our_server_name=self.our_server_name)
        self.request_metrics.start(
            self.start_time, name=servlet_name, method=self.get_method()
        )

        self.synapse_site.access_logger.debug(
            "%s - %s - Received request: %s %s",
            self.get_client_ip_if_available(),
            self.synapse_site.site_tag,
            self.get_method(),
            self.get_redacted_uri(),
        )

    def _finished_processing(self) -> None:
        """Log the completion of this request and update the metrics"""
        assert self.logcontext is not None
        assert self.finish_time is not None

        usage = self.logcontext.get_resource_usage()

        if self._processing_finished_time is None:
            # we completed the request without anything calling processing()
            self._processing_finished_time = time.time()

        # the time between receiving the request and the request handler finishing
        processing_time = self._processing_finished_time - self.start_time

        # the time between the request handler finishing and the response being sent
        # to the client (nb may be negative)
        response_send_time = self.finish_time - self._processing_finished_time

        user_agent = get_request_user_agent(self, "-")

        # int(self.code) looks redundant, because self.code is already an int.
        # But self.code might be an HTTPStatus (which inherits from int)---which has
        # a different string representation. So ensure we really have an integer.
        code = str(int(self.code))
        if not self.finished:
            # we didn't send the full response before we gave up (presumably because
            # the connection dropped)
            code += "!"

        log_level = logging.INFO if self._should_log_request() else logging.DEBUG

        # If this is a request where the target user doesn't match the user who
        # authenticated (e.g. and admin is puppetting a user) then we log both.
        requester, authenticated_entity = self.get_authenticated_entity()
        if authenticated_entity:
            requester = f"{authenticated_entity}|{requester}"

        self.synapse_site.access_logger.log(
            log_level,
            "%s - %s - {%s}"
            " Processed request: %.3fsec/%.3fsec (%.3fsec, %.3fsec) (%.3fsec/%.3fsec/%d)"
            ' %sB %s "%s %s %s" "%s" [%d dbevts]',
            self.get_client_ip_if_available(),
            self.synapse_site.site_tag,
            requester,
            processing_time,
            response_send_time,
            usage.ru_utime,
            usage.ru_stime,
            usage.db_sched_duration_sec,
            usage.db_txn_duration_sec,
            int(usage.db_txn_count),
            self.sentLength,
            code,
            self.get_method(),
            self.get_redacted_uri(),
            self.clientproto.decode("ascii", errors="replace"),
            user_agent,
            usage.evt_db_fetch_count,
        )

        # complete the opentracing span, if any.
        if self._opentracing_span:
            self._opentracing_span.finish()

        try:
            self.request_metrics.stop(self.finish_time, self.code, self.sentLength)
        except Exception as e:
            logger.warning("Failed to stop metrics: %r", e)

    def _should_log_request(self) -> bool:
        """Whether we should log at INFO that we processed the request."""
        if self.path == b"/health":
            return False

        if self.method == b"OPTIONS":
            return False

        return True

    def get_client_ip_if_available(self) -> str:
        """Logging helper. Return something useful when a client IP is not retrievable
        from a unix socket.

        In practice, this returns the socket file path on a SynapseRequest if using a
        unix socket and the normal IP address for TCP sockets.

        """
        # getClientAddress().host returns a proper IP address for a TCP socket. But
        # unix sockets have no concept of IP addresses or ports and return a
        # UNIXAddress containing a 'None' value. In order to get something usable for
        # logs(where this is used) get the unix socket file. getHost() returns a
        # UNIXAddress containing a value of the socket file and has an instance
        # variable of 'name' encoded as a byte string containing the path we want.
        # Decode to utf-8 so it looks nice.
        if isinstance(self.getClientAddress(), UNIXAddress):
            return self.getHost().name.decode("utf-8")
        else:
            return self.getClientAddress().host

    def request_info(self) -> "RequestInfo":
        h = self.getHeader(b"User-Agent")
        user_agent = h.decode("ascii", "replace") if h else None
        return RequestInfo(user_agent=user_agent, ip=self.get_client_ip_if_available())


class XForwardedForRequest(SynapseRequest):
    """Request object which honours proxy headers

    Extends SynapseRequest to replace getClientIP, getClientAddress, and isSecure with
    information from request headers.
    """

    # the client IP and ssl flag, as extracted from the headers.
    _forwarded_for: "_XForwardedForAddress | None" = None
    _forwarded_https: bool = False

    def requestReceived(self, command: bytes, path: bytes, version: bytes) -> None:
        # this method is called by the Channel once the full request has been
        # received, to dispatch the request to a resource.
        # We can use it to set the IP address and protocol according to the
        # headers.
        self._process_forwarded_headers()
        return super().requestReceived(command, path, version)

    def _process_forwarded_headers(self) -> None:
        headers = self.requestHeaders.getRawHeaders(b"x-forwarded-for")
        if not headers:
            return

        # for now, we just use the first x-forwarded-for header. Really, we ought
        # to start from the client IP address, and check whether it is trusted; if it
        # is, work backwards through the headers until we find an untrusted address.
        # see https://github.com/matrix-org/synapse/issues/9471
        self._forwarded_for = _XForwardedForAddress(
            headers[0].split(b",")[0].strip().decode("ascii")
        )

        # if we got an x-forwarded-for header, also look for an x-forwarded-proto header
        header = self.getHeader(b"x-forwarded-proto")
        if header is not None:
            self._forwarded_https = header.lower() == b"https"
        else:
            # this is done largely for backwards-compatibility so that people that
            # haven't set an x-forwarded-proto header don't get a redirect loop.
            logger.warning(
                "forwarded request lacks an x-forwarded-proto header: assuming https"
            )
            self._forwarded_https = True

    def isSecure(self) -> bool:
        if self._forwarded_https:
            return True
        return super().isSecure()

    def getClientIP(self) -> str:
        """
        Return the IP address of the client who submitted this request.

        This method is deprecated.  Use getClientAddress() instead.
        """
        if self._forwarded_for is not None:
            return self._forwarded_for.host
        return super().getClientIP()

    def getClientAddress(self) -> IAddress:
        """
        Return the address of the client who submitted this request.
        """
        if self._forwarded_for is not None:
            return self._forwarded_for
        return super().getClientAddress()


@implementer(IAddress)
@attr.s(frozen=True, slots=True, auto_attribs=True)
class _XForwardedForAddress:
    host: str


class SynapseProtocol(HTTPChannel):
    """
    Synapse-specific twisted http Protocol.

    This is a small wrapper around the twisted HTTPChannel so we can track active
    connections in order to close any outstanding connections on shutdown.
    """

    def __init__(
        self,
        site: "SynapseSite",
        our_server_name: str,
        max_request_body_size: int,
        request_id_header: str | None,
        request_class: type,
    ):
        super().__init__()
        self.factory: SynapseSite = site
        self.site = site
        self.our_server_name = our_server_name
        self.max_request_body_size = max_request_body_size
        self.request_id_header = request_id_header
        self.request_class = request_class

    def connectionMade(self) -> None:
        """
        Called when a connection is made.

        This may be considered the initializer of the protocol, because
        it is called when the connection is completed.

        Add the connection to the factory's connection list when it's established.
        """
        super().connectionMade()
        self.factory.addConnection(self)

    def connectionLost(self, reason: Failure) -> None:  # type: ignore[override]
        """
        Called when the connection is shut down.

        Clear any circular references here, and any external references to this
        Protocol. The connection has been closed. In our case, we need to remove the
        connection from the factory's connection list, when it's lost.
        """
        super().connectionLost(reason)
        self.factory.removeConnection(self)

    def requestFactory(self, http_channel: HTTPChannel, queued: bool) -> SynapseRequest:  # type: ignore[override]
        """
        A callable used to build `twisted.web.iweb.IRequest` objects.

        Use our own custom SynapseRequest type instead of the regular
        twisted.web.server.Request.
        """
        return self.request_class(
            self,
            self.factory,
            our_server_name=self.our_server_name,
            max_request_body_size=self.max_request_body_size,
            queued=queued,
            request_id_header=self.request_id_header,
        )


class SynapseSite(ProxySite):
    """
    Synapse-specific twisted http Site

    This does two main things.

    First, it replaces the requestFactory in use so that we build SynapseRequests
    instead of regular t.w.server.Requests. All of the  constructor params are really
    just parameters for SynapseRequest.

    Second, it inhibits the log() method called by Request.finish, since SynapseRequest
    does its own logging.
    """

    def __init__(
        self,
        *,
        logger_name: str,
        site_tag: str,
        config: ListenerConfig,
        resource: IResource,
        server_version_string: str,
        max_request_body_size: int,
        reactor: ISynapseReactor,
        hs: "HomeServer",
    ):
        """

        Args:
            logger_name:  The name of the logger to use for access logs.
            site_tag:  A tag to use for this site - mostly in access logs.
            config:  Configuration for the HTTP listener corresponding to this site
            resource:  The base of the resource tree to be used for serving requests on
                this site
            server_version_string: A string to present for the Server header
            max_request_body_size: Maximum request body length to allow before
                dropping the connection
            reactor: reactor to be used to manage connection timeouts
        """
        super().__init__(
            resource=resource,
            reactor=reactor,
            hs=hs,
        )

        self.site_tag = site_tag
        self.reactor: ISynapseReactor = reactor
        self.server_name = hs.hostname

        assert config.http_options is not None
        proxied = config.http_options.x_forwarded
        self.request_class = XForwardedForRequest if proxied else SynapseRequest

        self.request_id_header = config.http_options.request_id_header
        self.max_request_body_size = max_request_body_size

        self.access_logger = logging.getLogger(logger_name)
        self.server_version_string = server_version_string.encode("ascii")
        self.connections: list[Protocol] = []

    def buildProtocol(self, addr: IAddress) -> SynapseProtocol:
        protocol = SynapseProtocol(
            self,
            self.server_name,
            self.max_request_body_size,
            self.request_id_header,
            self.request_class,
        )
        return protocol

    def addConnection(self, protocol: Protocol) -> None:
        self.connections.append(protocol)

    def removeConnection(self, protocol: Protocol) -> None:
        if protocol in self.connections:
            self.connections.remove(protocol)

    def stopFactory(self) -> None:
        super().stopFactory()

        # Shutdown any connections which are still active.
        # These can be long lived HTTP connections which wouldn't normally be closed
        # when calling `shutdown` on the respective `Port`.
        # Closing the connections here is required for us to fully shutdown the
        # `SynapseHomeServer` in order for it to be garbage collected.
        for protocol in self.connections[:]:
            if protocol.transport is not None:
                protocol.transport.loseConnection()
        self.connections.clear()

    def log(self, request: SynapseRequest) -> None:  # type: ignore[override]
        pass


@attr.s(auto_attribs=True, frozen=True, slots=True)
class RequestInfo:
    user_agent: str | None
    ip: str
