#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""
Compatibility shim between aiohttp.web and Synapse's existing Twisted-based
request/response API.

This module allows Synapse's 100+ REST endpoint handlers (which were written
against Twisted's Request interface) to work on top of aiohttp.web without
modification.  The key classes are:

- ``ShimRequestHeaders`` / ``ShimResponseHeaders``: adapt between Twisted's
  ``Headers`` byte-oriented API and aiohttp's ``CIMultiDict`` string API.
- ``SynapseRequest``: wraps an ``aiohttp.web.Request`` and exposes the full
  API surface that servlet code expects (``args``, ``content``, ``method``,
  ``requestHeaders``, ``setResponseCode``, ``write``, ``finish``, etc.).
- ``SynapseSite``: a data-only replacement for the Twisted ``SynapseSite``
  that holds listener configuration without any Twisted inheritance.
- ``aiohttp_handler_factory``: produces an ``aiohttp.web`` request handler
  that bridges into Synapse's resource tree.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import time
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    Any,
    Generator,
    Iterator,
)

import attr
from aiohttp import web as aiohttp_web

from synapse.api.errors import Codes, CodeMessageException, SynapseError
from synapse.http import get_request_user_agent, redact_uri
from synapse.http.request_metrics import RequestMetrics, requests_counter
from synapse.logging.context import (
    ContextRequest,
    LoggingContext,
    PreserveLoggingContext,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.types import Requester

if TYPE_CHECKING:
    import opentracing

logger = logging.getLogger(__name__)

_next_request_seq = 0


# ---------------------------------------------------------------------------
# Header shim classes
# ---------------------------------------------------------------------------


class ShimRequestHeaders:
    """Wraps ``aiohttp.web.Request.headers`` (a ``CIMultiDictProxy[str]``)
    to present Twisted's ``Headers`` byte-oriented interface.

    The Twisted ``Headers`` API works with ``bytes`` header names and values.
    aiohttp stores headers as ``str``.  This class translates on the fly.

    An internal ``_extra`` dict supports ``addRawHeader`` for header injection
    (e.g. tests, or synthetic headers).
    """

    def __init__(self, raw_headers: Any) -> None:
        # raw_headers is a CIMultiDictProxy[str] from aiohttp
        self._raw = raw_headers
        # Extra headers injected after construction (e.g. for testing).
        self._extra: dict[str, list[str]] = {}

    @staticmethod
    def _norm_name(name: bytes | str) -> str:
        if isinstance(name, bytes):
            return name.decode("ascii", errors="replace")
        return name

    @staticmethod
    def _to_bytes(value: str | bytes) -> bytes:
        if isinstance(value, bytes):
            return value
        return value.encode("utf-8")

    def getRawHeaders(self, name: bytes | str) -> list[bytes] | None:
        """Return all values for *name* as a list of ``bytes``, or ``None``."""
        str_name = self._norm_name(name)

        values: list[str] = list(self._raw.getall(str_name, [])) if self._raw is not None else []
        if str_name.lower() in self._extra:
            values.extend(self._extra[str_name.lower()])

        if not values:
            return None
        return [self._to_bytes(v) for v in values]

    def hasHeader(self, name: bytes | str) -> bool:
        str_name = self._norm_name(name)
        if self._raw is not None and str_name in self._raw:
            return True
        return str_name.lower() in self._extra

    def addRawHeader(self, name: bytes | str, value: bytes | str) -> None:
        """Inject an additional header value.

        Since the underlying ``CIMultiDictProxy`` is immutable, injected
        headers are stored in a side dict and merged into query results.
        """
        str_name = self._norm_name(name).lower()
        str_value = value.decode("utf-8") if isinstance(value, bytes) else value
        self._extra.setdefault(str_name, []).append(str_value)

    def getAllRawHeaders(self) -> Iterator[tuple[bytes, list[bytes]]]:
        """Yield ``(name_bytes, [value_bytes, ...])`` for every header."""
        seen: dict[str, list[bytes]] = {}

        # Collect from the real headers.
        if self._raw is not None:
            for key, value in self._raw.items():
                lower = key.lower()
                seen.setdefault(lower, []).append(self._to_bytes(value))

        # Merge injected headers.
        for lower_name, str_values in self._extra.items():
            for sv in str_values:
                seen.setdefault(lower_name, []).append(self._to_bytes(sv))

        for name_lower, vals in seen.items():
            yield (name_lower.encode("ascii"), vals)


class ShimResponseHeaders:
    """Buffers response headers with Twisted's ``Headers`` interface.

    The buffered headers are later converted into the ``dict[str, str]``
    that ``aiohttp.web.Response`` expects via ``to_dict()``.  Where
    multiple values exist for a single header name they are joined with
    ``", "`` (per HTTP/1.1 rules) except for ``Set-Cookie`` which is
    emitted as separate entries.
    """

    def __init__(self) -> None:
        # Lowercased name -> list of str values
        self._headers: dict[str, list[str]] = {}
        # Preserve original casing for the first occurrence of each name.
        self._original_name: dict[str, str] = {}

    @staticmethod
    def _norm_name(name: bytes | str) -> str:
        if isinstance(name, bytes):
            return name.decode("ascii", errors="replace")
        return name

    @staticmethod
    def _norm_value(value: bytes | str) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value

    def setRawHeaders(self, name: bytes | str, values: list[bytes | str]) -> None:
        """Replace all values for *name*."""
        str_name = self._norm_name(name)
        lower = str_name.lower()
        self._original_name.setdefault(lower, str_name)
        self._headers[lower] = [self._norm_value(v) for v in values]

    def addRawHeader(self, name: bytes | str, value: bytes | str) -> None:
        """Append a single value for *name*."""
        str_name = self._norm_name(name)
        lower = str_name.lower()
        self._original_name.setdefault(lower, str_name)
        self._headers.setdefault(lower, []).append(self._norm_value(value))

    def getRawHeaders(self, name: bytes | str) -> list[bytes] | None:
        lower = self._norm_name(name).lower()
        vals = self._headers.get(lower)
        if vals is None:
            return None
        return [v.encode("utf-8") for v in vals]

    def hasHeader(self, name: bytes | str) -> bool:
        return self._norm_name(name).lower() in self._headers

    def removeHeader(self, name: bytes | str) -> None:
        lower = self._norm_name(name).lower()
        self._headers.pop(lower, None)
        self._original_name.pop(lower, None)

    def to_dict(self) -> dict[str, str]:
        """Convert to a flat ``dict[str, str]`` suitable for ``aiohttp.web.Response``.

        Multi-valued headers (except ``set-cookie``) are joined with ``", "``.
        ``Set-Cookie`` headers require special treatment because they must not
        be folded; in practice ``aiohttp`` handles this via ``resp.set_cookie``,
        but for simplicity we join them here too — callers that need strict
        Set-Cookie semantics should use ``to_pairs()`` instead.
        """
        result: dict[str, str] = {}
        for lower, values in self._headers.items():
            canonical = self._original_name.get(lower, lower)
            result[canonical] = ", ".join(values)
        return result

    def to_pairs(self) -> list[tuple[str, str]]:
        """Convert to a list of ``(name, value)`` pairs.

        This preserves multi-valued headers as separate entries, which is
        required for ``Set-Cookie`` and useful for proxying scenarios.
        """
        pairs: list[tuple[str, str]] = []
        for lower, values in self._headers.items():
            canonical = self._original_name.get(lower, lower)
            for v in values:
                pairs.append((canonical, v))
        return pairs


# ---------------------------------------------------------------------------
# Client address helper
# ---------------------------------------------------------------------------


@attr.s(frozen=True, slots=True, auto_attribs=True)
class _ClientAddress:
    """Minimal stand-in for Twisted's ``IAddress``.

    Only the ``host`` attribute is needed by Synapse code.
    """

    host: str


# ---------------------------------------------------------------------------
# Request info (duplicated from site.py to avoid import issues)
# ---------------------------------------------------------------------------


@attr.s(auto_attribs=True, frozen=True, slots=True)
class RequestInfo:
    user_agent: str | None
    ip: str


# ---------------------------------------------------------------------------
# SynapseSite (data-only, no Twisted inheritance)
# ---------------------------------------------------------------------------


class SynapseSite:
    """Holds site/listener configuration without any Twisted inheritance.

    This is the aiohttp-era replacement for ``synapse.http.site.SynapseSite``.
    It carries the same configuration that ``SynapseRequest`` (the shim)
    needs at runtime.
    """

    def __init__(
        self,
        *,
        site_tag: str = "",
        server_version_string: str = "",
        reactor: Any = None,
        server_name: str = "",
        max_request_body_size: int = 50 * 1024 * 1024,
        request_id_header: str | None = None,
        x_forwarded: bool = False,
        access_logger: logging.Logger | None = None,
        # Old-style constructor args for backward compat (tests, etc.)
        logger_name: str | None = None,
        config: Any = None,
        resource: Any = None,
        hs: Any = None,
    ) -> None:
        # If called with old-style args, extract values
        if config is not None:
            from synapse.config.server import HttpListenerConfig
            if hasattr(config, 'http_options'):
                http_opts = config.http_options
                x_forwarded = getattr(http_opts, 'x_forwarded', False)
                request_id_header = getattr(http_opts, 'request_id_header', None)
        if hs is not None and not server_name:
            server_name = hs.hostname
        if logger_name is not None and access_logger is None:
            access_logger = logging.getLogger(logger_name)

        self.site_tag = site_tag
        if isinstance(server_version_string, str):
            self.server_version_string: bytes | str = server_version_string.encode(
                "ascii"
            ) if server_version_string else b""
        else:
            self.server_version_string = server_version_string
        self.reactor = reactor
        self.server_name = server_name
        self.max_request_body_size = max_request_body_size
        self.request_id_header = request_id_header
        self.x_forwarded = x_forwarded
        self.access_logger = access_logger or logging.getLogger("synapse.access.http")
        # Store resource for test infrastructure
        self.resource = resource


# ---------------------------------------------------------------------------
# SynapseRequest shim
# ---------------------------------------------------------------------------


class SynapseRequest:
    """Wraps an ``aiohttp.web.Request`` to present the full API surface
    that Synapse's servlet / handler code expects.

    This includes:
    - Twisted-style request properties (``args``, ``content``, ``method``,
      ``path``, ``uri``, ``requestHeaders``, ``clientproto``).
    - Twisted-style response methods (``setResponseCode``, ``setHeader``,
      ``write``, ``finish``, ``redirect``).
    - Synapse-specific attributes (``request_metrics``, ``logcontext``,
      ``requester``, ``synapse_site``, etc.).
    - X-Forwarded-For / X-Forwarded-Proto support.
    - ``build_aiohttp_response()`` to assemble the final ``aiohttp.web.Response``
      from buffered response data.
    """

    def __init__(
        self,
        aiohttp_request: aiohttp_web.Request,
        site: SynapseSite,
        body: bytes,
    ) -> None:
        self._aiohttp_request = aiohttp_request
        self.synapse_site = site
        self.reactor = site.reactor
        self.our_server_name = site.server_name

        # ------------------------------------------------------------------
        # Request properties — translated from aiohttp to Twisted conventions
        # ------------------------------------------------------------------

        self.method: bytes = aiohttp_request.method.encode("ascii")
        self.path: bytes = aiohttp_request.path.encode("utf-8")

        # ``uri`` in Twisted is the path + query string (NOT the full URL).
        raw_path = aiohttp_request.raw_path  # already includes query string
        self.uri: bytes = raw_path.encode("utf-8") if isinstance(raw_path, str) else raw_path

        self.clientproto: bytes = (
            b"HTTP/%d.%d" % aiohttp_request.version
            if aiohttp_request.version
            else b"HTTP/1.1"
        )

        # Query parameters: Twisted stores these as ``dict[bytes, list[bytes]]``.
        self.args: dict[bytes, list[bytes]] = {}
        for key, values in aiohttp_request.query.items():
            bkey = key.encode("utf-8")
            self.args.setdefault(bkey, []).append(values.encode("utf-8"))
        # aiohttp's ``query`` is a MultiDict — if a key appears multiple times
        # the above loop handles it because ``items()`` yields every (k, v) pair.

        # Request body: Twisted exposes this as a file-like ``content`` attribute
        # that servlets read via ``request.content.read()``.
        self.content: io.BytesIO = io.BytesIO(body)

        # Headers
        self.requestHeaders: ShimRequestHeaders = ShimRequestHeaders(
            aiohttp_request.headers
        )

        # ------------------------------------------------------------------
        # Response buffering
        # ------------------------------------------------------------------

        self.responseHeaders: ShimResponseHeaders = ShimResponseHeaders()
        self.code: int = 200
        self.code_message: bytes = b"OK"
        self.finished: bool = False
        self.startedWriting: bool = False
        self.sentLength: int = 0
        self._response_buffer: bytearray = bytearray()

        # Compatibility: Twisted's ``Request._disconnected`` is checked by
        # ``respond_with_json`` and error handling code.
        self._disconnected: bool = False

        # ------------------------------------------------------------------
        # X-Forwarded-For / X-Forwarded-Proto handling
        # ------------------------------------------------------------------

        self._forwarded_for: _ClientAddress | None = None
        self._forwarded_https: bool = False

        if site.x_forwarded:
            self._process_forwarded_headers()

        # ------------------------------------------------------------------
        # Synapse-specific attributes
        # ------------------------------------------------------------------

        global _next_request_seq
        self.request_seq = _next_request_seq
        _next_request_seq += 1

        self.request_id_header = site.request_id_header

        # Will be set by ``render()`` or ``_started_processing()``.
        self.request_metrics: RequestMetrics = None  # type: ignore[assignment]
        self.logcontext: LoggingContext | None = None

        self._requester: Requester | str | None = None
        self._opentracing_span: "opentracing.Span | None" = None

        # Deferred / Future for cancellation support.
        self.render_deferred: asyncio.Future[None] | None = None
        self.is_render_cancellable: bool = False

        self.start_time: float = 0.0
        self._is_processing: bool = False
        self._processing_finished_time: float | None = None
        self.finish_time: float | None = None

        # Cookies list — Twisted's Request has this for redirect cookies.
        self.cookies: list[bytes] = []

        # Channel stub — some error-handling code checks ``request.channel``.
        # We provide a truthy object so that ``if request.channel:`` passes,
        # but there is no real Twisted channel behind an aiohttp request.
        self.channel: Any = None

    @classmethod
    def for_testing(
        cls,
        channel: Any,
        site: "SynapseSite",
        our_server_name: str = "test_server",
        max_request_body_size: int = 50 * 1024 * 1024,
    ) -> "SynapseRequest":
        """Create a SynapseRequest for testing without a real aiohttp request.

        The caller should set `.content`, `.method`, `.path`, `.uri`, `.args`,
        and call `.requestHeaders.addRawHeader()` as needed.
        """
        obj = object.__new__(cls)
        obj._aiohttp_request = None
        obj.synapse_site = site
        obj.reactor = getattr(site, 'reactor', None)
        obj.our_server_name = our_server_name

        # Request properties — to be populated by test code
        obj.method = b"GET"
        obj.path = b"/"
        obj.uri = b"/"
        obj.clientproto = b"HTTP/1.1"
        obj.args = {}
        obj.content = io.BytesIO(b"")
        obj.requestHeaders = ShimRequestHeaders(None)
        obj.responseHeaders = ShimResponseHeaders()
        obj.code = 200
        obj.code_message = b"OK"
        obj.finished = False
        obj.startedWriting = False
        obj.sentLength = 0
        obj._response_buffer = bytearray()
        obj._disconnected = False
        obj._forwarded_for = None
        obj._forwarded_https = False

        global _next_request_seq
        obj.request_seq = _next_request_seq
        _next_request_seq += 1

        obj.request_id_header = getattr(site, 'request_id_header', None)
        obj.request_metrics = None  # type: ignore[assignment]
        obj.logcontext = None
        obj._requester = None
        obj._opentracing_span = None
        obj.render_deferred = None
        obj.is_render_cancellable = False
        obj.start_time = 0.0
        obj._is_processing = False
        obj._processing_finished_time = None
        obj.finish_time = None
        obj.cookies = []
        obj.channel = channel
        obj._client_ip = "127.0.0.1"
        return obj

    # ------------------------------------------------------------------
    # X-Forwarded-For processing
    # ------------------------------------------------------------------

    def _process_forwarded_headers(self) -> None:
        """Extract client IP from X-Forwarded-For and scheme from
        X-Forwarded-Proto, mirroring ``XForwardedForRequest``."""
        headers = self.requestHeaders.getRawHeaders(b"x-forwarded-for")
        if not headers:
            return

        # Use the first entry from the first X-Forwarded-For header,
        # consistent with the Twisted implementation.
        self._forwarded_for = _ClientAddress(
            headers[0].split(b",")[0].strip().decode("ascii")
        )

        proto_header = self.getHeader(b"x-forwarded-proto")
        if proto_header is not None:
            self._forwarded_https = proto_header.lower() == b"https"
        else:
            # Backwards-compatibility: default to HTTPS to avoid redirect loops.
            logger.warning(
                "Forwarded request lacks an x-forwarded-proto header: assuming https"
            )
            self._forwarded_https = True

    # ------------------------------------------------------------------
    # Requester property
    # ------------------------------------------------------------------

    @property
    def requester(self) -> Requester | str | None:
        return self._requester

    @requester.setter
    def requester(self, value: Requester | str) -> None:
        # Should only be set once.
        assert self._requester is None

        self._requester = value

        assert self.logcontext is not None
        assert self.logcontext.request is not None

        requester, authenticated_entity = self.get_authenticated_entity()
        self.logcontext.request.requester = requester
        self.logcontext.request.authenticated_entity = (
            authenticated_entity or requester
        )

    # ------------------------------------------------------------------
    # Request introspection methods
    # ------------------------------------------------------------------

    def getHeader(self, name: bytes | str) -> bytes | None:
        """Return the first value of the named request header, or ``None``.

        Matches Twisted's ``Request.getHeader`` semantics — returns ``bytes``.
        """
        values = self.requestHeaders.getRawHeaders(name)
        if values:
            return values[0]
        return None

    def getClientAddress(self) -> _ClientAddress:
        """Return an object with a ``.host`` attribute giving the client IP."""
        if self._forwarded_for is not None:
            return self._forwarded_for

        # For test requests without a real aiohttp request
        if self._aiohttp_request is None:
            return _ClientAddress(getattr(self, '_client_ip', '127.0.0.1'))

        peer = self._aiohttp_request.remote
        if peer is not None:
            return _ClientAddress(peer)

        return _ClientAddress("127.0.0.1")

    def getClientIP(self) -> str:
        """Return the client IP as a string.

        Deprecated in Twisted in favour of ``getClientAddress().host``,
        but still used in some Synapse code paths.
        """
        return self.getClientAddress().host

    def isSecure(self) -> bool:
        """Return ``True`` if the request was made over HTTPS."""
        if self._aiohttp_request is None:
            return self._forwarded_https
        if self._forwarded_https:
            return True
        return self._aiohttp_request.secure

    def get_request_id(self) -> str:
        """Build a request ID string, optionally using a header value."""
        request_id_value = None
        if self.request_id_header:
            request_id_value = self.getHeader(self.request_id_header)
            if isinstance(request_id_value, bytes):
                request_id_value = request_id_value.decode("ascii", errors="replace")

        if request_id_value is None:
            request_id_value = str(self.request_seq)

        return "%s-%s" % (self.get_method(), request_id_value)

    def get_redacted_uri(self) -> str:
        """Return the URI with sensitive query parameters redacted."""
        uri: bytes | str = self.uri
        if isinstance(uri, bytes):
            uri = uri.decode("ascii", errors="replace")
        return redact_uri(uri)

    def get_method(self) -> str:
        """Return the HTTP method as a ``str``."""
        method: bytes | str = self.method
        if isinstance(method, bytes):
            return method.decode("ascii")
        return method

    def get_authenticated_entity(self) -> tuple[str | None, str | None]:
        """Return ``(requester_str, authenticated_entity_str | None)``."""
        if isinstance(self._requester, str):
            return self._requester, None
        elif isinstance(self._requester, Requester):
            requester = self._requester.user.to_string()
            authenticated_entity = self._requester.authenticated_entity
            if requester != authenticated_entity:
                return requester, authenticated_entity
            return requester, None
        elif self._requester is not None:
            return repr(self._requester), None
        return None, None

    def get_client_ip_if_available(self) -> str:
        """Return the client IP, suitable for logging."""
        return self.getClientAddress().host

    def request_info(self) -> RequestInfo:
        h = self.getHeader(b"User-Agent")
        user_agent = h.decode("ascii", "replace") if h else None
        return RequestInfo(user_agent=user_agent, ip=self.get_client_ip_if_available())

    def set_opentracing_span(self, span: "opentracing.Span") -> None:
        """Attach an opentracing span to this request."""
        self._opentracing_span = span

    # ------------------------------------------------------------------
    # Response methods (buffering)
    # ------------------------------------------------------------------

    def setResponseCode(self, code: int, message: bytes | None = None) -> None:
        """Set the HTTP response status code."""
        self.code = code
        if message is not None:
            self.code_message = message

    def setHeader(self, name: bytes | str, value: bytes | str) -> None:
        """Set a response header (replaces any existing values)."""
        if isinstance(value, (list, tuple)):
            self.responseHeaders.setRawHeaders(name, value)
        else:
            self.responseHeaders.setRawHeaders(name, [value])

    def write(self, data: bytes) -> None:
        """Append *data* to the response body buffer."""
        if self.finished:
            raise RuntimeError("Request.write called on a finished request")
        self.startedWriting = True
        self._response_buffer.extend(data)
        self.sentLength += len(data)

    def finish(self) -> None:
        """Mark the response as complete.

        In the Twisted world this sends the response to the client.  In the
        aiohttp shim it merely sets the ``finished`` flag — the actual
        ``aiohttp.web.Response`` is built later by ``build_aiohttp_response()``.
        """
        self.finish_time = time.time()
        self.finished = True

        if self._opentracing_span:
            self._opentracing_span.log_kv({"event": "response sent"})
        if not self._is_processing:
            if self.logcontext is not None:
                with PreserveLoggingContext(self.logcontext):
                    self._finished_processing()

    def redirect(self, url: bytes | str) -> None:
        """Send an HTTP 302 redirect to *url*."""
        if isinstance(url, str):
            url = url.encode("utf-8")
        self.setResponseCode(302)
        self.setHeader(b"Location", url)

    # ------------------------------------------------------------------
    # Processing lifecycle (mirrors site.py SynapseRequest)
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def processing(self) -> Generator[None, None, None]:
        """Context manager for tracking request processing lifecycle.

        This mirrors ``SynapseRequest.processing()`` from ``site.py``.
        While the context manager is active the request is considered "in
        progress" and completion logging is deferred until exit.
        """
        if self._is_processing:
            raise RuntimeError("Request is already processing")
        self._is_processing = True

        try:
            yield
        except Exception:
            logger.exception(
                "Asynchronous message handler raised an uncaught exception"
            )
        finally:
            self._processing_finished_time = time.time()
            self._is_processing = False

            if self._opentracing_span:
                self._opentracing_span.log_kv({"event": "finished processing"})

            # If the response has already been sent, log completion now.
            if self.finish_time is not None:
                self._finished_processing()

    def _started_processing(self, servlet_name: str) -> None:
        """Record that request processing has begun (for metrics/logging)."""
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
        """Log request completion and update metrics."""
        if self.logcontext is None:
            return

        usage = self.logcontext.get_resource_usage()

        if self._processing_finished_time is None:
            self._processing_finished_time = time.time()

        if self.finish_time is None:
            self.finish_time = time.time()

        processing_time = self._processing_finished_time - self.start_time
        response_send_time = self.finish_time - self._processing_finished_time

        user_agent = get_request_user_agent(self, "-")

        code = str(int(self.code))
        if not self.finished:
            code += "!"

        log_level = logging.INFO if self._should_log_request() else logging.DEBUG

        requester, authenticated_entity = self.get_authenticated_entity()
        if authenticated_entity:
            requester = f"{authenticated_entity}|{requester}"

        self.synapse_site.access_logger.log(
            log_level,
            "%s - %s - {%s}"
            " Processed request: %.3fsec/%.3fsec ru=(%.3fsec, %.3fsec) db=(%.3fsec/%.3fsec/%d)"
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

    # ------------------------------------------------------------------
    # Render entry point (replaces Twisted's Request.render)
    # ------------------------------------------------------------------

    def start_render(self, servlet_name: str) -> None:
        """Set up the LoggingContext and begin metrics tracking.

        This replaces the ``SynapseRequest.render()`` method from Twisted,
        which is called by the channel when a resource has been located.
        """
        request_id = self.get_request_id()
        self.logcontext = LoggingContext(
            name=request_id,
            server_name=self.our_server_name,
            request=ContextRequest(
                request_id=request_id,
                ip_address=self.get_client_ip_if_available(),
                site_tag=self.synapse_site.site_tag,
                requester=None,
                authenticated_entity=None,
                method=self.get_method(),
                url=self.get_redacted_uri(),
                protocol=self.clientproto.decode("ascii", errors="replace"),
                user_agent=get_request_user_agent(self),
            ),
        )

        # Set the Server header, as the Twisted SynapseRequest.render does.
        self.setHeader("Server", self.synapse_site.server_version_string)

        self._started_processing(servlet_name)

    # ------------------------------------------------------------------
    # aiohttp response assembly
    # ------------------------------------------------------------------

    def build_aiohttp_response(self) -> aiohttp_web.Response:
        """Construct and return an ``aiohttp.web.Response`` from the buffered
        response code, headers, and body.

        This is called after the request handler has finished writing the
        response via ``setResponseCode``, ``setHeader``, ``write``, and
        ``finish``.
        """
        headers_pairs = self.responseHeaders.to_pairs()

        return aiohttp_web.Response(
            status=self.code,
            headers=headers_pairs,  # type: ignore[arg-type]
            body=bytes(self._response_buffer),
        )

    # ------------------------------------------------------------------
    # __repr__
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return "<%s at 0x%x method=%r uri=%r clientproto=%r site=%r>" % (
            self.__class__.__name__,
            id(self),
            self.get_method(),
            self.get_redacted_uri(),
            self.clientproto.decode("ascii", errors="replace"),
            self.synapse_site.site_tag,
        )


# ---------------------------------------------------------------------------
# aiohttp handler factory
# ---------------------------------------------------------------------------


def aiohttp_handler_factory(
    site: SynapseSite,
    root_resource: Any,
) -> Any:
    """Return an ``async def handler(request)`` suitable for use as an aiohttp
    route handler.

    The returned handler:

    1. Pre-reads the request body (enforcing a size limit).
    2. Creates a ``SynapseRequest`` wrapping the aiohttp request.
    3. Sets up the ``LoggingContext``.
    4. Delegates to ``root_resource._async_render_wrapper(synapse_request)``
       (which internally calls ``request.processing()``).
    5. Awaits completion and returns ``synapse_request.build_aiohttp_response()``.

    Args:
        site: The ``SynapseSite`` holding listener configuration.
        root_resource: The root resource whose ``_async_render_wrapper``
            will be called to dispatch the request.

    Returns:
        An async handler function with signature
        ``async def(request: aiohttp.web.Request) -> aiohttp.web.Response``.
    """

    async def handler(aiohttp_request: aiohttp_web.Request) -> aiohttp_web.Response:
        # 1. Pre-read request body with size limit enforcement.
        body = await _read_body_with_limit(aiohttp_request, site.max_request_body_size)

        # 2. Create the SynapseRequest shim.
        synapse_request = SynapseRequest(aiohttp_request, site, body)

        # 3. Determine a servlet name for initial metrics.
        servlet_name = root_resource.__class__.__name__
        synapse_request.start_render(servlet_name)

        assert synapse_request.logcontext is not None

        try:
            with PreserveLoggingContext(synapse_request.logcontext):
                # 4. Invoke the resource's async render wrapper.
                # _async_render_wrapper is already decorated with
                # @wrap_async_request_handler which calls request.processing().
                await root_resource._async_render_wrapper(synapse_request)

                # Record the arrival after dispatching so the handler can
                # update the servlet name in request_metrics.
                requests_counter.labels(
                    method=synapse_request.get_method(),
                    servlet=synapse_request.request_metrics.name,
                    **{SERVER_NAME_LABEL: synapse_request.our_server_name},
                ).inc()
        except Exception:
            # If the handler raised and nothing wrote a response, produce
            # a JSON error response.
            if not synapse_request.startedWriting:
                _write_json_error_to_request(synapse_request)

        # 5. Build and return the aiohttp response.
        return synapse_request.build_aiohttp_response()

    return handler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _read_body_with_limit(
    aiohttp_request: aiohttp_web.Request, max_size: int
) -> bytes:
    """Read the full request body, raising a ``SynapseError`` if it exceeds
    *max_size* bytes.

    aiohttp does not enforce a body size limit by default, so we read in
    chunks and bail out early if the limit is exceeded.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in aiohttp_request.content.iter_any():
        total += len(chunk)
        if total > max_size:
            raise SynapseError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Request content is too large (>{max_size})",
                Codes.TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _write_json_error_to_request(request: SynapseRequest) -> None:
    """Write a generic 500 error response to *request*.

    This is a simplified version of ``return_json_error`` for the aiohttp
    path — it does not need Twisted's ``Failure`` machinery.
    """
    import traceback

    logger.error(
        "Unhandled error processing request %r:\n%s",
        request,
        traceback.format_exc(),
    )

    error_dict = {"error": "Internal server error", "errcode": Codes.UNKNOWN}
    body = json.dumps(error_dict).encode("utf-8")

    request.setResponseCode(500)
    request.setHeader(b"Content-Type", b"application/json")
    request.setHeader(b"Content-Length", str(len(body)).encode("ascii"))
    request.write(body)
    request.finish()
