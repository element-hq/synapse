#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""asyncio-native HTTP server using aiohttp.web.

Phase 5 of the Twisted → asyncio migration. Provides:

- NativeSynapseRequest: compatibility shim wrapping aiohttp.web.Request with
  the Twisted-style interface that existing servlets expect.
- NativeJsonResource: aiohttp-based request router implementing register_paths()
  with the same dispatch semantics as JsonResource.
- Native response functions (respond_with_json_native, etc.)

This module is unused until later phases switch the listener setup to use it.
"""

import inspect
import json
import logging
import re
import urllib.parse
from http import HTTPStatus
from io import BytesIO
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Pattern,
)

import aiohttp.web
from canonicaljson import encode_canonical_json

from synapse.api.errors import (
    Codes,
    RedirectException,
    SynapseError,
    UnrecognizedRequestError,
)

logger = logging.getLogger(__name__)


# Type alias matching the existing servlet callback signature
ServletCallback = Callable[..., Any]


# ============================================================================
# NativeSynapseRequest — Twisted compatibility shim
# ============================================================================


class _NativeHeaders:
    """Minimal compatibility shim for Twisted's Headers interface."""

    def __init__(self, headers: Any) -> None:
        self._headers = headers

    def getRawHeaders(
        self, name: str | bytes, default: list[bytes] | None = None
    ) -> list[bytes] | None:
        key = name.decode("ascii") if isinstance(name, bytes) else name
        values = self._headers.getall(key, None)
        if values is None:
            return default
        return [v.encode("utf-8") for v in values]

    def hasHeader(self, name: str | bytes) -> bool:
        key = name.decode("ascii") if isinstance(name, bytes) else name
        return key in self._headers

    def getAllRawHeaders(self) -> dict[bytes, list[bytes]]:
        result: dict[bytes, list[bytes]] = {}
        for key in self._headers:
            encoded_key = key.encode("ascii")
            result.setdefault(encoded_key, []).extend(
                v.encode("utf-8") for v in self._headers.getall(key)
            )
        return result


class _NativeResponseHeaders:
    """Minimal compatibility shim for response headers."""

    def __init__(self) -> None:
        self._headers: dict[str, str] = {}

    def hasHeader(self, name: str | bytes) -> bool:
        key = name.decode("ascii") if isinstance(name, bytes) else name
        return key in self._headers

    def addRawHeader(self, name: str | bytes, value: str | bytes) -> None:
        key = name.decode("ascii") if isinstance(name, bytes) else name
        val = value.decode("utf-8") if isinstance(value, bytes) else value
        self._headers[key] = val


class NativeSynapseRequest:
    """Compatibility shim wrapping aiohttp.web.Request.

    Exposes the subset of the Twisted Request interface that Synapse servlets
    actually use, allowing ~221 servlets to work without modification.

    Key Twisted-compatible attributes/methods:
    - .method (bytes)
    - .uri (bytes)
    - .path (bytes)
    - .args (dict[bytes, list[bytes]])
    - .content (BytesIO)
    - .requestHeaders
    - .responseHeaders
    - .setResponseCode(code)
    - .setHeader(name, value)
    - .getClientAddress()
    """

    def __init__(self, aiohttp_request: aiohttp.web.Request, body: bytes) -> None:
        self._request = aiohttp_request
        self._body = body

        # Twisted-compatible attributes
        self.method: bytes = aiohttp_request.method.encode("ascii")
        self.uri: bytes = str(aiohttp_request.url.relative()).encode("ascii")
        self.path: bytes = aiohttp_request.path.encode("ascii")

        # Query args: Twisted uses dict[bytes, list[bytes]]
        self.args: dict[bytes, list[bytes]] = {}
        for key, value in aiohttp_request.query.items():
            bkey = key.encode("utf-8")
            self.args.setdefault(bkey, []).append(value.encode("utf-8"))

        # Request body as a BytesIO stream
        self.content = BytesIO(body)

        # Headers
        self.requestHeaders = _NativeHeaders(aiohttp_request.headers)
        self.responseHeaders = _NativeResponseHeaders()

        # Response state
        self._response_code: int = 200
        self._response_headers: dict[str, str] = {}
        self._response_body_parts: list[bytes] = []
        self._finished = False
        self._disconnected = False
        self.startedWriting = False

        # Compatibility attributes used by some code paths
        self.requester: Any = None
        self.is_render_cancellable = False
        self.request_metrics: Any = None

    def setResponseCode(self, code: int) -> None:
        self._response_code = code

    def setHeader(self, name: bytes | str, value: bytes | str) -> None:
        key = name.decode("ascii") if isinstance(name, bytes) else name
        val = value.decode("utf-8") if isinstance(value, bytes) else value
        self._response_headers[key] = val
        self.responseHeaders.addRawHeader(name, value)

    def write(self, data: bytes) -> None:
        self.startedWriting = True
        self._response_body_parts.append(data)

    def finish(self) -> None:
        self._finished = True

    def getClientAddress(self) -> Any:
        """Return the client's address."""
        peername = self._request.transport.get_extra_info("peername") if self._request.transport else None
        if peername:
            return _SimpleAddress(peername[0], peername[1])
        return _SimpleAddress("0.0.0.0", 0)

    def get_method(self) -> str:
        return self._request.method

    def get_redacted_uri(self) -> str:
        uri = str(self._request.url.relative())
        return re.sub(r"(\?.*access_token=)[^&]*", r"\1<redacted>", uri)

    def build_response(self) -> aiohttp.web.Response:
        """Build the aiohttp.web.Response from accumulated state."""
        body = b"".join(self._response_body_parts)
        return aiohttp.web.Response(
            status=self._response_code,
            headers=self._response_headers,
            body=body,
        )


class _SimpleAddress:
    """Minimal address object for getClientAddress() compatibility."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.type = "TCP"


# ============================================================================
# Response helper functions
# ============================================================================


def respond_with_json_native(
    code: int,
    json_object: Any,
    send_cors: bool = False,
    canonical_json: bool = True,
) -> aiohttp.web.Response:
    """Build an aiohttp JSON response.

    This is the asyncio-native equivalent of respond_with_json().
    Instead of writing to a Twisted Request, it returns an aiohttp.web.Response.
    """
    if canonical_json:
        json_bytes = encode_canonical_json(json_object)
    else:
        json_bytes = json.dumps(json_object).encode("utf-8")

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache, no-store, must-revalidate",
    }

    if send_cors:
        headers.update(_cors_headers())

    return aiohttp.web.Response(
        status=code,
        body=json_bytes,
        headers=headers,
    )


def respond_with_html_native(
    code: int,
    html: str,
) -> aiohttp.web.Response:
    """Build an aiohttp HTML response."""
    return aiohttp.web.Response(
        status=code,
        body=html.encode("utf-8"),
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none';",
        },
    )


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": (
            "X-Requested-With, Content-Type, Authorization, Date"
        ),
    }


def _error_response(
    code: int,
    msg: str,
    errcode: str = Codes.UNKNOWN,
    send_cors: bool = True,
) -> aiohttp.web.Response:
    """Build a JSON error response."""
    return respond_with_json_native(
        code,
        {"errcode": errcode, "error": msg},
        send_cors=send_cors,
    )


# ============================================================================
# NativeJsonResource — aiohttp-based request router
# ============================================================================


class _PathEntry:
    __slots__ = ["callback", "servlet_classname"]

    def __init__(self, callback: ServletCallback, servlet_classname: str) -> None:
        self.callback = callback
        self.servlet_classname = servlet_classname


class NativeJsonResource:
    """asyncio-native equivalent of JsonResource.

    Implements register_paths() with the same API so existing RestServlet.register()
    calls work without modification. Builds an aiohttp.web.Application with a
    catch-all route that dispatches based on the registered path patterns.

    Usage:
        resource = NativeJsonResource(server_name="my.server")
        my_servlet.register(resource)  # RestServlet.register() calls register_paths()
        app = resource.build_app()
        runner = aiohttp.web.AppRunner(app)
    """

    def __init__(self, server_name: str) -> None:
        self._server_name = server_name
        # Mapping from compiled pattern → method (bytes) → _PathEntry
        self._routes: dict[Pattern[str], dict[bytes, _PathEntry]] = {}

    def register_paths(
        self,
        method: str,
        path_patterns: Iterable[Pattern[str]],
        callback: ServletCallback,
        servlet_classname: str,
    ) -> None:
        """Register a handler for the given HTTP method and path patterns.

        Same interface as JsonResource.register_paths().
        """
        method_bytes = method.encode("ascii")
        for pattern in path_patterns:
            entry_map = self._routes.setdefault(pattern, {})
            entry_map[method_bytes] = _PathEntry(callback, servlet_classname)

    def build_app(self) -> aiohttp.web.Application:
        """Build an aiohttp.web.Application with all registered routes."""
        app = aiohttp.web.Application()
        # Catch-all route that dispatches via our pattern matching
        app.router.add_route("*", "/{path_info:.*}", self._handle_request)
        return app

    async def _handle_request(
        self, aiohttp_request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Main request handler — matches path patterns and dispatches."""
        # Handle OPTIONS for CORS preflight
        if aiohttp_request.method == "OPTIONS":
            return aiohttp.web.Response(
                status=204,
                headers=_cors_headers(),
            )

        request_path = aiohttp_request.path
        method = aiohttp_request.method.encode("ascii")

        # Read body once
        body = await aiohttp_request.read()

        # Create compatibility shim
        request = NativeSynapseRequest(aiohttp_request, body)

        try:
            # Find matching handler
            callback, path_kwargs = self._get_handler(request_path, method)

            # URL-decode path parameters
            kwargs = {
                name: urllib.parse.unquote(value)
                for name, value in path_kwargs.items()
            }

            # Call the handler
            result = callback(request, **kwargs)

            # Handle async results
            if inspect.isawaitable(result):
                result = await result

            # Handlers return tuple[int, JsonDict] or None
            if result is not None:
                code, response_object = result
                return respond_with_json_native(
                    code, response_object, send_cors=True
                )

            # If handler returned None, it may have written to the request shim
            if request._finished or request.startedWriting:
                return request.build_response()

            # No response generated
            return _error_response(500, "Handler returned no response")

        except RedirectException as e:
            location = e.location.decode("utf-8") if isinstance(e.location, bytes) else e.location
            resp_headers: dict[str, str] = {"Location": location}
            if e.cookies:
                for cookie in e.cookies:
                    cookie_str = cookie.decode("utf-8") if isinstance(cookie, bytes) else cookie
                    resp_headers["Set-Cookie"] = cookie_str
            return aiohttp.web.Response(
                status=e.code,
                headers=resp_headers,
                body=b"",
            )
        except SynapseError as e:
            error_dict = e.error_dict(None)
            return respond_with_json_native(
                e.code, error_dict, send_cors=True
            )
        except Exception:
            logger.exception("Unexpected error handling %s %s", aiohttp_request.method, request_path)
            return _error_response(
                500,
                "Internal server error",
                errcode=Codes.UNKNOWN,
            )

    def _get_handler(
        self, request_path: str, method: bytes
    ) -> tuple[ServletCallback, dict[str, str]]:
        """Find the handler for a request path and method.

        Returns:
            Tuple of (callback, path_kwargs).

        Raises:
            UnrecognizedRequestError: If no pattern matches (404) or
                method is not allowed for the matched pattern (405).
        """
        for pattern, methods in self._routes.items():
            m = pattern.match(request_path)
            if m:
                entry = methods.get(method)
                if not entry:
                    # Pattern matched but method not registered
                    raise UnrecognizedRequestError(code=405)
                return entry.callback, m.groupdict()

        raise UnrecognizedRequestError(code=404)
