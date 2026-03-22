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

import abc
import asyncio
import html
import logging
import urllib
import urllib.parse
from http import HTTPStatus
from http.client import FOUND
from inspect import isawaitable
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Iterable,
    Pattern,
    Protocol,
)

import jinja2
from canonicaljson import encode_canonical_json

from asyncio import CancelledError

from synapse.types import ISynapseThreadlessReactor

try:
    from twisted.internet import reactor as _twisted_reactor
    from twisted.web import resource
    from twisted.web.pages import notFound
except ImportError:
    _twisted_reactor = None  # type: ignore[assignment]
    resource = None  # type: ignore[assignment]
    notFound = None  # type: ignore[assignment]

from synapse.api.errors import (
    CodeMessageException,
    Codes,
    LimitExceededError,
    RedirectException,
    SynapseError,
    UnrecognizedRequestError,
)
from synapse.config.homeserver import HomeServerConfig
from synapse.logging.opentracing import trace_servlet
from synapse.util.caches import intern_dict
from synapse.util.cancellation import is_function_cancellable
from synapse.util.clock import Clock
from synapse.util.duration import Duration
from synapse.util.json import json_encoder

if TYPE_CHECKING:
    from synapse.http.aiohttp_shim import SynapseRequest
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

HTML_ERROR_TEMPLATE = """<!DOCTYPE html>
<html lang=en>
  <head>
    <meta charset="utf-8">
    <title>Error {code}</title>
  </head>
  <body>
     <p>{msg}</p>
  </body>
</html>
"""

# A fictional HTTP status code for requests where the client has disconnected and we
# successfully cancelled the request. Used only for logging purposes. Clients will never
# observe this code unless cancellations leak across requests or we raise a
# `CancelledError` ourselves.
# Analogous to nginx's 499 status code:
# https://github.com/nginx/nginx/blob/release-1.21.6/src/http/ngx_http_request.h#L128-L134
HTTP_STATUS_REQUEST_CANCELLED = 499


def return_json_error(
    exc: Exception, request: "SynapseRequest", config: HomeServerConfig | None
) -> None:
    """Sends a JSON error response to clients.

    Args:
        exc: The exception that caused the error.
        request: The request to respond to.
        config: The homeserver config, or None.
    """

    if isinstance(exc, SynapseError):
        error_code = exc.code
        error_dict = exc.error_dict(config)
        if exc.headers is not None:
            for header, value in exc.headers.items():
                request.setHeader(header, value)
        error_ctx = exc.debug_context
        if error_ctx:
            logger.info(
                "%s SynapseError: %s - %s (%s)", request, error_code, exc.msg, error_ctx
            )
        else:
            logger.info("%s SynapseError: %s - %s", request, error_code, exc.msg)
    elif isinstance(exc, CancelledError):
        error_code = HTTP_STATUS_REQUEST_CANCELLED
        error_dict = {"error": "Request cancelled", "errcode": Codes.UNKNOWN}

        if not request._disconnected:
            logger.error(
                "Got cancellation before client disconnection from %r: %r",
                request.request_metrics.name,
                request,
                exc_info=True,
            )
    else:
        error_code = 500
        error_dict = {"error": "Internal server error", "errcode": Codes.UNKNOWN}

        logger.error(
            "Failed handle request via %r: %r",
            request.request_metrics.name,
            request,
            exc_info=True,
        )

    # Only respond with an error response if we haven't already started writing,
    # otherwise lets just kill the connection
    if request.startedWriting:
        # In aiohttp world, there's no channel to force-abort — the response
        # is buffered and we can't retract it.  Just log.
        logger.warning(
            "Error occurred after response writing started for %r", request
        )
    else:
        respond_with_json(
            request,
            error_code,
            error_dict,
            send_cors=True,
        )


def return_html_error(
    exc: Exception,
    request: "SynapseRequest",
    error_template: str | jinja2.Template,
) -> None:
    """Sends an HTML error page corresponding to the given exception.

    Handles RedirectException and other CodeMessageExceptions (such as SynapseError)

    Args:
        exc: the error to report
        request: the failing request
        error_template: the HTML template. Can be either a string (with `{code}`,
            `{msg}` placeholders), or a jinja2 template
    """
    if isinstance(exc, CodeMessageException):
        code = exc.code
        msg = exc.msg
        if exc.headers is not None:
            for header, value in exc.headers.items():
                request.setHeader(header, value)

        if isinstance(exc, RedirectException):
            logger.info("%s redirect to %s", request, exc.location)
            request.setHeader(b"location", exc.location)
            request.cookies.extend(exc.cookies)
        elif isinstance(exc, SynapseError):
            logger.info("%s SynapseError: %s - %s", request, code, msg)
        else:
            logger.error(
                "Failed handle request %r",
                request,
                exc_info=True,
            )
    elif isinstance(exc, CancelledError):
        code = HTTP_STATUS_REQUEST_CANCELLED
        msg = "Request cancelled"

        if not request._disconnected:
            logger.error(
                "Got cancellation before client disconnection when handling request %r",
                request,
                exc_info=True,
            )
    else:
        code = HTTPStatus.INTERNAL_SERVER_ERROR
        msg = "Internal server error"

        logger.error(
            "Failed handle request %r",
            request,
            exc_info=True,
        )

    if isinstance(error_template, str):
        body = error_template.format(code=code, msg=html.escape(msg))
    else:
        body = error_template.render(code=code, msg=msg)

    respond_with_html(request, code, body)


def wrap_async_request_handler(
    h: Callable[["_AsyncResource", "SynapseRequest"], Awaitable[None]],
) -> Callable[["_AsyncResource", "SynapseRequest"], Awaitable[None]]:
    """Wraps an async request handler so that it calls request.processing.

    This helps ensure that work done by the request handler after the request is completed
    is correctly recorded against the request metrics/logs.

    The handler method must have a signature of "handle_foo(self, request)",
    where "request" must be a SynapseRequest.

    The handler may return a coroutine, in which case the completion of the request isn't
    logged until the coroutine completes.
    """

    async def wrapped_async_request_handler(
        self: "_AsyncResource", request: "SynapseRequest"
    ) -> None:
        with request.processing():
            await h(self, request)

    # Return the async function directly — no preserve_fn wrapping needed
    # since the aiohttp handler factory awaits this directly.
    return wrapped_async_request_handler


# Type of a callback method for processing requests
# it is actually called with a SynapseRequest and a kwargs dict for the params,
# but I can't figure out how to represent that.
ServletCallback = Callable[
    ..., None | Awaitable[None] | tuple[int, Any] | Awaitable[tuple[int, Any]]
]


class HttpServer(Protocol):
    """Interface for registering callbacks on a HTTP server"""

    def register_paths(
        self,
        method: str,
        path_patterns: Iterable[Pattern[str]],
        callback: ServletCallback,
        servlet_classname: str,
    ) -> None:
        """Register a callback that gets fired if we receive a http request
        with the given method for a path that matches the given regex.

        If the regex contains groups these gets passed to the callback via
        an unpacked tuple.

        The callback may be marked with the `@cancellable` decorator, which will
        cause request processing to be cancelled when clients disconnect early.

        Args:
            method: The HTTP method to listen to.
            path_patterns: The regex used to match requests.
            callback: The function to fire if we receive a matched
                request. The first argument will be the request object and
                subsequent arguments will be any matched groups from the regex.
                This should return either tuple of (code, response), or None.
            servlet_classname: The name of the handler to be used in prometheus
                and opentracing logs.
        """


class _AsyncResource(metaclass=abc.ABCMeta):
    """Base class for resources that have async handlers.

    Sub classes can either implement `_async_render_<METHOD>` to handle
    requests by method, or override `_async_render` to handle all requests.

    Args:
        extract_context: Whether to attempt to extract the opentracing
            context from the request the servlet is handling.
    """

    def __init__(self, clock: Clock, extract_context: bool = False):
        self._clock = clock
        self._extract_context = extract_context

    @wrap_async_request_handler
    async def _async_render_wrapper(self, request: "SynapseRequest") -> None:
        """This is a wrapper that delegates to `_async_render` and handles
        exceptions, return values, metrics, etc.
        """
        try:
            request.request_metrics.name = self.__class__.__name__

            with trace_servlet(request, self._extract_context):
                try:
                    callback_return = await self._async_render(request)
                except LimitExceededError as e:
                    if e.pause:
                        # Use real asyncio.sleep for the anti-hammering pause,
                        # not fake-time clock.sleep, so tests don't hang.
                        await asyncio.sleep(e.pause)
                    raise

                if callback_return is not None:
                    code, response = callback_return
                    self._send_response(request, code, response)
        except Exception as e:
            self._send_error_response(e, request)

    async def _async_render(self, request: "SynapseRequest") -> tuple[int, Any] | None:
        """Delegates to `_async_render_<METHOD>` methods, or returns a 400 if
        no appropriate method exists. Can be overridden in sub classes for
        different routing.
        """
        # Treat HEAD requests as GET requests.
        request_method = request.method.decode("ascii")
        if request_method == "HEAD":
            request_method = "GET"

        method_handler = getattr(self, "_async_render_%s" % (request_method,), None)
        if method_handler:
            request.is_render_cancellable = is_function_cancellable(method_handler)

            raw_callback_return = method_handler(request)

            # Is it synchronous? We'll allow this for now.
            if isawaitable(raw_callback_return):
                callback_return = await raw_callback_return
            else:
                callback_return = raw_callback_return

            return callback_return

        # A request with an unknown method (for a known endpoint) was received.
        raise UnrecognizedRequestError(code=405)

    @abc.abstractmethod
    def _send_response(
        self,
        request: "SynapseRequest",
        code: int,
        response_object: Any,
    ) -> None:
        raise NotImplementedError()

    @abc.abstractmethod
    def _send_error_response(
        self,
        exc: Exception,
        request: "SynapseRequest",
    ) -> None:
        raise NotImplementedError()


class DirectServeJsonResource(_AsyncResource):
    """A resource that will call `self._async_on_<METHOD>` on new requests,
    formatting responses and errors as JSON.
    """

    def __init__(
        self,
        canonical_json: bool = False,
        extract_context: bool = False,
        # Clock is optional as this class is exposed to the module API.
        clock: Clock | None = None,
    ):
        """
        Args:
            canonical_json: TODO
            extract_context: TODO
            clock: This is expected to be passed in by any Synapse code.
                Only optional for the Module API.
        """

        if clock is None:
            # Ideally we wouldn't ignore the linter error here and instead enforce a
            # required `Clock` be passed into the `__init__` function.
            # However, this would change the function signature which is currently being
            # exported to the module api. Since we don't want to break that api, we have
            # to settle with ignoring the linter error here.
            # As of the time of writing this, all Synapse internal usages of
            # `DirectServeJsonResource` pass in the existing homeserver clock instance.
            clock = Clock(  # type: ignore[multiple-internal-clocks]
                ISynapseThreadlessReactor(_twisted_reactor),
                server_name="synapse_module_running_from_unknown_server",
            )

        super().__init__(clock, extract_context)
        self.canonical_json = canonical_json

    def _send_response(
        self,
        request: "SynapseRequest",
        code: int,
        response_object: Any,
    ) -> None:
        """Implements _AsyncResource._send_response"""
        # TODO: Only enable CORS for the requests that need it.
        respond_with_json(
            request,
            code,
            response_object,
            send_cors=True,
            canonical_json=self.canonical_json,
        )

    def _send_error_response(
        self,
        exc: Exception,
        request: "SynapseRequest",
    ) -> None:
        """Implements _AsyncResource._send_error_response"""
        return_json_error(exc, request, None)


class _PathEntry:
    __slots__ = ("callback", "servlet_classname")

    def __init__(self, callback: ServletCallback, servlet_classname: str):
        self.callback = callback
        self.servlet_classname = servlet_classname


class JsonResource(DirectServeJsonResource):
    """This implements the HttpServer interface and provides JSON support for
    Resources.

    Register callbacks via register_paths()

    Callbacks can return a tuple of status code and a dict in which case the
    the dict will automatically be sent to the client as a JSON object.

    The JsonResource is primarily intended for returning JSON, but callbacks
    may send something other than JSON, they may do so by using the methods
    on the request object and instead returning None.
    """

    isLeaf = True

    def __init__(
        self,
        hs: "HomeServer",
        canonical_json: bool = True,
        extract_context: bool = False,
    ):
        self.clock = hs.get_clock()
        super().__init__(canonical_json, extract_context, clock=self.clock)
        # Map of path regex -> method -> callback.
        self._routes: dict[Pattern[str], dict[bytes, _PathEntry]] = {}
        self.hs = hs

    def register_paths(
        self,
        method: str,
        path_patterns: Iterable[Pattern[str]],
        callback: ServletCallback,
        servlet_classname: str,
    ) -> None:
        """
        Registers a request handler against a regular expression. Later request URLs are
        checked against these regular expressions in order to identify an appropriate
        handler for that request.

        Args:
            method: GET, POST etc

            path_patterns: A list of regular expressions to which the request
                URLs are compared.

            callback: The handler for the request. Usually a Servlet

            servlet_classname: The name of the handler to be used in prometheus
                and opentracing logs.
        """
        method_bytes = method.encode("utf-8")

        for path_pattern in path_patterns:
            logger.debug("Registering for %s %s", method, path_pattern.pattern)
            self._routes.setdefault(path_pattern, {})[method_bytes] = _PathEntry(
                callback, servlet_classname
            )

    def _get_handler_for_request(
        self, request: "SynapseRequest"
    ) -> tuple[ServletCallback, str, dict[str, str]]:
        """Finds a callback method to handle the given request.

        Returns:
            A tuple of the callback to use, the name of the servlet, and the
            key word arguments to pass to the callback
        """
        # At this point the path must be bytes.
        request_path_bytes: bytes = request.path
        request_path = request_path_bytes.decode("ascii")
        # Treat HEAD requests as GET requests.
        request_method = request.method
        if request_method == b"HEAD":
            request_method = b"GET"

        # Loop through all the registered callbacks to check if the method
        # and path regex match
        for path_pattern, methods in self._routes.items():
            m = path_pattern.match(request_path)
            if m:
                # We found a matching path!
                path_entry = methods.get(request_method)
                if not path_entry:
                    raise UnrecognizedRequestError(code=405)
                return path_entry.callback, path_entry.servlet_classname, m.groupdict()

        # Huh. No one wanted to handle that? Fiiiiiine.
        raise UnrecognizedRequestError(code=404)

    async def _async_render(self, request: "SynapseRequest") -> tuple[int, Any]:
        callback, servlet_classname, group_dict = self._get_handler_for_request(request)

        request.is_render_cancellable = is_function_cancellable(callback)

        # Make sure we have an appropriate name for this handler in prometheus
        # (rather than the default of JsonResource).
        request.request_metrics.name = servlet_classname

        # Now trigger the callback. If it returns a response, we send it
        # here. If it throws an exception, that is handled by the wrapper
        # installed by @request_handler.
        kwargs = intern_dict(
            {
                name: urllib.parse.unquote(value) if value else value
                for name, value in group_dict.items()
            }
        )

        raw_callback_return = callback(request, **kwargs)

        # Is it synchronous? We'll allow this for now.
        if isawaitable(raw_callback_return):
            callback_return = await raw_callback_return
        else:
            callback_return = raw_callback_return

        return callback_return

    def _send_error_response(
        self,
        exc: Exception,
        request: "SynapseRequest",
    ) -> None:
        """Implements _AsyncResource._send_error_response"""
        return_json_error(exc, request, self.hs.config)


class DirectServeHtmlResource(_AsyncResource):
    """A resource that will call `self._async_on_<METHOD>` on new requests,
    formatting responses and errors as HTML.
    """

    # The error template to use for this resource
    ERROR_TEMPLATE = HTML_ERROR_TEMPLATE

    def __init__(
        self,
        extract_context: bool = False,
        # Clock is optional as this class is exposed to the module API.
        clock: Clock | None = None,
    ):
        """
        Args:
            extract_context: TODO
            clock: This is expected to be passed in by any Synapse code.
                Only optional for the Module API.
        """
        if clock is None:
            # Ideally we wouldn't ignore the linter error here and instead enforce a
            # required `Clock` be passed into the `__init__` function.
            # However, this would change the function signature which is currently being
            # exported to the module api. Since we don't want to break that api, we have
            # to settle with ignoring the linter error here.
            # As of the time of writing this, all Synapse internal usages of
            # `DirectServeHtmlResource` pass in the existing homeserver clock instance.
            clock = Clock(  # type: ignore[multiple-internal-clocks]
                ISynapseThreadlessReactor(_twisted_reactor),
                server_name="synapse_module_running_from_unknown_server",
            )

        super().__init__(clock, extract_context)

    def _send_response(
        self,
        request: "SynapseRequest",
        code: int,
        response_object: Any,
    ) -> None:
        """Implements _AsyncResource._send_response"""
        # We expect to get bytes for us to write
        assert isinstance(response_object, bytes)
        html_bytes = response_object

        respond_with_html_bytes(request, code, html_bytes)

    def _send_error_response(
        self,
        exc: Exception,
        request: "SynapseRequest",
    ) -> None:
        """Implements _AsyncResource._send_error_response"""
        return_html_error(exc, request, self.ERROR_TEMPLATE)


try:
    from twisted.web.static import File as _TwistedFile
    _StaticBase = _TwistedFile
except ImportError:
    _StaticBase = object  # type: ignore[assignment,misc]

from synapse.http.resource import Resource as _ResourceBase


class StaticResource(_StaticBase):
    """
    A resource that represents a plain non-interpreted file or directory.

    Differs from the File resource by adding clickjacking protection.
    """

    def render_GET(self, request: Any) -> bytes:
        set_clickjacking_protection_headers(request)
        return super().render_GET(request)

    def directoryListing(self) -> Any:
        if notFound is not None:
            return notFound()
        return None


class UnrecognizedRequestResource(_ResourceBase):
    """
    Similar to twisted.web.resource.NoResource, but returns a JSON 404 with an
    errcode of M_UNRECOGNIZED.
    """

    def render(self, request: "SynapseRequest") -> bytes:
        exc = UnrecognizedRequestError(code=404)
        return_json_error(exc, request, None)
        # Return empty bytes — the response has already been written to the request.
        return b""

    def getChild(self, name: str, request: Any) -> Any:
        return self


class RootRedirect(_ResourceBase):
    """Redirects the root '/' path to another path."""

    def __init__(self, path: str):
        super().__init__()
        self.url = path

    def render_GET(self, request: Any) -> bytes:
        from twisted.web.util import redirectTo
        return redirectTo(self.url.encode("ascii"), request)

    def getChild(self, name: str, request: Any) -> Any:
        if len(name) == 0:
            return self  # select ourselves as the child to render
        return super().getChild(name, request)


class OptionsResource(_ResourceBase):
    """Responds to OPTION requests for itself and all children."""

    def render_OPTIONS(self, request: "SynapseRequest") -> bytes:
        request.setResponseCode(204)
        request.setHeader(b"Content-Length", b"0")

        set_cors_headers(request)

        return b""

    def getChildWithDefault(self, path: str, request: Any) -> Any:
        if request.method == b"OPTIONS":
            return self  # select ourselves as the child to render
        return super().getChildWithDefault(path, request)


class RootOptionsRedirectResource(OptionsResource, RootRedirect):
    pass


def _encode_json_bytes(json_object: object) -> bytes:
    """
    Encode an object into JSON. Returns bytes.
    """
    return json_encoder.encode(json_object).encode("utf-8")


def respond_with_json(
    request: "SynapseRequest",
    code: int,
    json_object: Any,
    send_cors: bool = False,
    canonical_json: bool = True,
) -> None:
    """Sends encoded JSON in response to the given request.

    Args:
        request: The http request to respond to.
        code: The HTTP response code.
        json_object: The object to serialize to JSON.
        send_cors: Whether to send Cross-Origin Resource Sharing headers
            https://fetch.spec.whatwg.org/#http-cors-protocol
        canonical_json: Whether to use the canonicaljson algorithm when encoding
            the JSON bytes.
    """
    # The response code must always be set, for logging purposes.
    request.setResponseCode(code)

    if request._disconnected:
        logger.warning(
            "Not sending response to request %s, already disconnected.", request
        )
        return

    if canonical_json:
        encoder: Callable[[object], bytes] = encode_canonical_json
    else:
        encoder = _encode_json_bytes

    request.setHeader(b"Content-Type", b"application/json")
    # Insert a default Cache-Control header if the servlet hasn't already set one. The
    # default directive tells both the client and any intermediary cache to not cache
    # the response, which is a sensible default to have on most API endpoints.
    # The absence `Cache-Control` header would mean that it's up to the clients and
    # caching proxies mood to cache things if they want. This can be dangerous, which is
    # why we explicitly set a "don't cache by default" policy.
    # In practice, `no-store` should be enough, but having all three directives is more
    # conservative in case we encounter weird, non-spec compliant caches.
    # See https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control#directives
    # for more details.
    if not request.responseHeaders.hasHeader(b"Cache-Control"):
        request.setHeader(b"Cache-Control", b"no-cache, no-store, must-revalidate")

    if send_cors:
        set_cors_headers(request)

    # Encode and write the JSON directly — response is buffered in the shim.
    json_bytes = encoder(json_object)
    request.setHeader(b"Content-Length", b"%d" % (len(json_bytes),))
    request.write(json_bytes)
    finish_request(request)


def respond_with_json_bytes(
    request: "SynapseRequest",
    code: int,
    json_bytes: bytes,
    send_cors: bool = False,
) -> None:
    """Sends encoded JSON in response to the given request.

    Args:
        request: The http request to respond to.
        code: The HTTP response code.
        json_bytes: The json bytes to use as the response body.
        send_cors: Whether to send Cross-Origin Resource Sharing headers
            https://fetch.spec.whatwg.org/#http-cors-protocol
    """
    # The response code must always be set, for logging purposes.
    request.setResponseCode(code)

    if request._disconnected:
        logger.warning(
            "Not sending response to request %s, already disconnected.", request
        )
        return

    request.setHeader(b"Content-Type", b"application/json")
    request.setHeader(b"Content-Length", b"%d" % (len(json_bytes),))
    # Insert a default Cache-Control header if the servlet hasn't already set one. The
    # default directive tells both the client and any intermediary cache to not cache
    # the response, which is a sensible default to have on most API endpoints.
    # The absence `Cache-Control` header would mean that it's up to the clients and
    # caching proxies mood to cache things if they want. This can be dangerous, which is
    # why we explicitly set a "don't cache by default" policy.
    # In practice, `no-store` should be enough, but having all three directives is more
    # conservative in case we encounter weird, non-spec compliant caches.
    # See https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Cache-Control#directives
    # for more details.
    if not request.responseHeaders.hasHeader(b"Cache-Control"):
        request.setHeader(b"Cache-Control", b"no-cache, no-store, must-revalidate")

    if send_cors:
        set_cors_headers(request)

    request.write(json_bytes)
    finish_request(request)


def set_cors_headers(request: "SynapseRequest") -> None:
    """Set the CORS headers so that javascript running in a web browsers can
    use this API

    Args:
        request: The http request to add CORS to.
    """
    request.setHeader(b"Access-Control-Allow-Origin", b"*")
    request.setHeader(
        b"Access-Control-Allow-Methods", b"GET, HEAD, POST, PUT, DELETE, OPTIONS"
    )
    if request.path is not None and (
        request.path == b"/_matrix/client/unstable/org.matrix.msc4108/rendezvous"
        or request.path.startswith(b"/_synapse/client/rendezvous")
    ):
        request.setHeader(
            b"Access-Control-Allow-Headers",
            b"Content-Type, If-Match, If-None-Match",
        )
        request.setHeader(
            b"Access-Control-Expose-Headers",
            b"Synapse-Trace-Id, Server, ETag",
        )
    else:
        request.setHeader(
            b"Access-Control-Allow-Headers",
            b"X-Requested-With, Content-Type, Authorization, Date",
        )
        request.setHeader(
            b"Access-Control-Expose-Headers",
            b"Synapse-Trace-Id, Server",
        )


def set_corp_headers(request: "SynapseRequest") -> None:
    """Set the CORP headers so that javascript running in a web browsers can
    embed the resource returned from this request when their client requires
    the `Cross-Origin-Embedder-Policy: require-corp` header.

    Args:
        request: The http request to add the CORP header to.
    """
    request.setHeader(b"Cross-Origin-Resource-Policy", b"cross-origin")


def respond_with_html(request: "SynapseRequest", code: int, html: str) -> None:
    """
    Wraps `respond_with_html_bytes` by first encoding HTML from a str to UTF-8 bytes.
    """
    respond_with_html_bytes(request, code, html.encode("utf-8"))


def respond_with_html_bytes(request: "SynapseRequest", code: int, html_bytes: bytes) -> None:
    """
    Sends HTML (encoded as UTF-8 bytes) as the response to the given request.

    Note that this adds clickjacking protection headers and finishes the request.

    Args:
        request: The http request to respond to.
        code: The HTTP response code.
        html_bytes: The HTML bytes to use as the response body.
    """
    # The response code must always be set, for logging purposes.
    request.setResponseCode(code)

    if request._disconnected:
        logger.warning(
            "Not sending response to request %s, already disconnected.", request
        )
        return None

    request.setHeader(b"Content-Type", b"text/html; charset=utf-8")
    request.setHeader(b"Content-Length", b"%d" % (len(html_bytes),))

    # Ensure this content cannot be embedded.
    set_clickjacking_protection_headers(request)

    request.write(html_bytes)
    finish_request(request)


def set_clickjacking_protection_headers(request: "SynapseRequest") -> None:
    """
    Set headers to guard against clickjacking of embedded content.

    This sets the X-Frame-Options and Content-Security-Policy headers which instructs
    browsers to not allow the HTML of the response to be embedded onto another
    page.

    Args:
        request: The http request to add the headers to.
    """
    request.setHeader(b"X-Frame-Options", b"DENY")
    request.setHeader(b"Content-Security-Policy", b"frame-ancestors 'none';")


def respond_with_redirect(
    request: "SynapseRequest", url: bytes, statusCode: int = FOUND, cors: bool = False
) -> None:
    """
    Write a 302 (or other specified status code) response to the request, if it is still alive.

    Args:
        request: The http request to respond to.
        url: The URL to redirect to.
        statusCode: The HTTP status code to use for the redirect (defaults to 302).
        cors: Whether to set CORS headers on the response.
    """
    logger.debug("Redirect to %s", url.decode("utf-8"))

    if cors:
        set_cors_headers(request)

    request.setResponseCode(statusCode)
    request.setHeader(b"location", url)
    finish_request(request)


def finish_request(request: "SynapseRequest") -> None:
    """Finish writing the response to the request.

    Catches RuntimeError in case the request has already been finished or the
    connection was closed.
    """
    try:
        request.finish()
    except RuntimeError as e:
        logger.info("Connection disconnected before response was written: %r", e)
