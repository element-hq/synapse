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

"""asyncio-native HTTP client using aiohttp.

Phase 4 of the Twisted → asyncio migration. Provides NativeSimpleHttpClient
as a replacement for SimpleHttpClient, using aiohttp.ClientSession instead
of treq + Twisted Agent.

This module is unused until later phases switch callers to use it.
"""

import asyncio
import logging
import ssl
import urllib.parse
from http import HTTPStatus
from io import BytesIO
from typing import (
    Any,
    BinaryIO,
    Callable,
    Mapping,
)

import aiohttp
from canonicaljson import encode_canonical_json
from netaddr import AddrFormatError, IPAddress, IPSet
from prometheus_client import Counter

from synapse.api.errors import Codes, HttpResponseException, SynapseError
from synapse.http.client import (
    QueryParams,
    RawHeaders,
    encode_query_args,
    redact_uri,
)

logger = logging.getLogger(__name__)

# Reuse metrics from the existing client module where possible
outgoing_requests_counter = Counter(
    "synapse_http_client_native_requests",
    "Number of outgoing HTTP requests via native client",
    ["method"],
)  # type: ignore[missing-server-name-label]

incoming_responses_counter = Counter(
    "synapse_http_client_native_responses",
    "Number of incoming HTTP responses via native client",
    ["method", "code"],
)  # type: ignore[missing-server-name-label]

# Maximum time to wait for response headers (seconds)
_DEFAULT_REQUEST_TIMEOUT = 60

# Maximum time to wait for file download body (seconds)
_DEFAULT_DOWNLOAD_TIMEOUT = 30

import json as json_decoder


def _is_ip_blocked(
    ip_address: IPAddress,
    ip_allowlist: IPSet | None,
    ip_blocklist: IPSet,
) -> bool:
    """Check if an IP address is blocked."""
    if ip_allowlist and ip_address in ip_allowlist:
        return False
    return ip_address in ip_blocklist


class _BlocklistingResolver(aiohttp.abc.AbstractResolver):
    """An aiohttp DNS resolver that filters out blocked IP addresses.

    This prevents DNS rebinding attacks by checking resolved addresses
    against blocklist/allowlist before returning them.
    """

    def __init__(
        self,
        ip_allowlist: IPSet | None,
        ip_blocklist: IPSet,
    ) -> None:
        self._inner = aiohttp.DefaultResolver()
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist

    async def resolve(
        self, host: str, port: int = 0, family: int = 0
    ) -> list[Any]:
        results = await self._inner.resolve(host, port, family)  # type: ignore[arg-type]

        filtered = []
        for result in results:
            try:
                ip = IPAddress(result["host"])
            except (AddrFormatError, ValueError, KeyError, TypeError):
                filtered.append(result)
                continue

            if _is_ip_blocked(ip, self._ip_allowlist, self._ip_blocklist):
                logger.info(
                    "Blocked %s from DNS resolution to %s", ip, host
                )
            else:
                filtered.append(result)

        if not filtered and results:
            # All IPs were blocked
            raise OSError(f"DNS resolution for {host} blocked by IP blocklist")

        return filtered

    async def close(self) -> None:
        await self._inner.close()


class NativeSimpleHttpClient:
    """asyncio-native HTTP client using aiohttp.ClientSession.

    Provides the same public interface as SimpleHttpClient but uses aiohttp
    instead of treq + Twisted Agent. Supports IP blocklisting via a custom
    DNS resolver, proxy support via aiohttp's built-in proxy capabilities,
    and connection pooling via aiohttp's internal connection pool.

    This is the Phase 4 asyncio-native equivalent of SimpleHttpClient.
    """

    def __init__(
        self,
        user_agent: str,
        ip_allowlist: IPSet | None = None,
        ip_blocklist: IPSet | None = None,
        proxy_url: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = 100,
        connection_timeout: float = 15.0,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        """
        Args:
            user_agent: User-Agent header value.
            ip_allowlist: IP addresses to allow even if in blocklist.
            ip_blocklist: IP addresses to disallow.
            proxy_url: HTTP proxy URL (e.g., "http://proxy:8080").
            ssl_context: SSL context for TLS connections.
            max_connections: Max persistent connections per host.
            connection_timeout: Timeout for establishing connections.
            request_timeout: Default timeout for request headers.
        """
        self.user_agent = user_agent
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist or IPSet()
        self._proxy_url = proxy_url
        self._request_timeout = request_timeout

        # Build connector with optional IP blocklisting resolver
        resolver = None
        if ip_blocklist:
            resolver = _BlocklistingResolver(ip_allowlist, ip_blocklist)

        connector = aiohttp.TCPConnector(
            limit_per_host=max_connections,
            resolver=resolver,
            ssl=ssl_context or False,
        )

        timeout = aiohttp.ClientTimeout(
            sock_connect=connection_timeout,
            total=None,  # We manage total timeout per-request
        )

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": user_agent},
        )

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        await self._session.close()

    async def request(
        self,
        method: str,
        uri: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> aiohttp.ClientResponse:
        """Make an HTTP request.

        Args:
            method: HTTP method (GET, POST, PUT, etc.)
            uri: Full URI to request.
            data: Request body bytes.
            headers: Additional request headers.

        Returns:
            aiohttp.ClientResponse with headers read.

        Raises:
            asyncio.TimeoutError: if the request times out.
        """
        # Check IP literal blocklisting
        parsed = urllib.parse.urlparse(uri)
        if parsed.hostname and self._ip_blocklist:
            try:
                ip = IPAddress(parsed.hostname)
                if _is_ip_blocked(ip, self._ip_allowlist, self._ip_blocklist):
                    raise SynapseError(
                        HTTPStatus.FORBIDDEN, "IP address blocked"
                    )
            except AddrFormatError:
                pass  # Not an IP literal

        outgoing_requests_counter.labels(method=method).inc()
        logger.debug("Sending request %s %s", method, redact_uri(uri))

        try:
            response = await asyncio.wait_for(
                self._session.request(
                    method,
                    uri,
                    data=data,
                    headers=headers,
                    proxy=self._proxy_url,
                ),
                timeout=self._request_timeout,
            )

            incoming_responses_counter.labels(
                method=method, code=response.status
            ).inc()
            logger.info(
                "Received response to %s %s: %s",
                method,
                redact_uri(uri),
                response.status,
            )
            return response

        except asyncio.TimeoutError:
            incoming_responses_counter.labels(method=method, code="ERR").inc()
            logger.info(
                "Timeout sending request to %s %s", method, redact_uri(uri)
            )
            raise
        except Exception as e:
            incoming_responses_counter.labels(method=method, code="ERR").inc()
            logger.info(
                "Error sending request to %s %s: %s %s",
                method,
                redact_uri(uri),
                type(e).__name__,
                str(e),
            )
            raise

    async def get_json(
        self,
        uri: str,
        args: QueryParams | None = None,
        headers: RawHeaders | None = None,
    ) -> Any:
        """GET JSON from a URI.

        Args:
            uri: The URI to request, not including query parameters.
            args: Query string parameters.
            headers: Additional headers.

        Returns:
            Parsed JSON response body.

        Raises:
            HttpResponseException: On non-2xx response.
            ValueError: If response is not valid JSON.
        """
        if args:
            query_str = urllib.parse.urlencode(args, True)
            uri = "%s?%s" % (uri, query_str)

        h = self._build_headers(headers, accept="application/json")
        response = await self.request("GET", uri, headers=h)
        body = await response.read()

        if 200 <= response.status < 300:
            return json_decoder.loads(body)
        else:
            raise HttpResponseException(
                response.status,
                response.reason or "",
                body,
            )

    async def post_json_get_json(
        self,
        uri: str,
        post_json: Any,
        headers: RawHeaders | None = None,
    ) -> Any:
        """POST JSON and get JSON response.

        Args:
            uri: URI to POST to.
            post_json: Object to JSON-encode as request body.
            headers: Additional headers.

        Returns:
            Parsed JSON response body.

        Raises:
            HttpResponseException: On non-2xx response.
            ValueError: If response is not valid JSON.
        """
        json_bytes = encode_canonical_json(post_json)

        h = self._build_headers(
            headers,
            content_type="application/json",
            accept="application/json",
        )
        response = await self.request("POST", uri, data=json_bytes, headers=h)
        body = await response.read()

        if 200 <= response.status < 300:
            return json_decoder.loads(body)
        else:
            raise HttpResponseException(
                response.status,
                response.reason or "",
                body,
            )

    async def post_urlencoded_get_json(
        self,
        uri: str,
        args: Mapping[str, str | list[str]] | None = None,
        headers: RawHeaders | None = None,
    ) -> Any:
        """POST URL-encoded form data and get JSON response.

        Args:
            uri: URI to POST to.
            args: Form parameters.
            headers: Additional headers.

        Returns:
            Parsed JSON response body.

        Raises:
            HttpResponseException: On non-2xx response.
        """
        query_bytes = encode_query_args(args)

        h = self._build_headers(
            headers,
            content_type="application/x-www-form-urlencoded",
            accept="application/json",
        )
        response = await self.request("POST", uri, data=query_bytes, headers=h)
        body = await response.read()

        if 200 <= response.status < 300:
            return json_decoder.loads(body)
        else:
            raise HttpResponseException(
                response.status,
                response.reason or "",
                body,
            )

    async def put_json(
        self,
        uri: str,
        json_body: Any,
        args: QueryParams | None = None,
        headers: RawHeaders | None = None,
    ) -> Any:
        """PUT JSON to a URI.

        Args:
            uri: URI to PUT to.
            json_body: Object to JSON-encode as request body.
            args: Query string parameters.
            headers: Additional headers.

        Returns:
            Parsed JSON response body.

        Raises:
            HttpResponseException: On non-2xx response.
        """
        if args:
            query_str = urllib.parse.urlencode(args, True)
            uri = "%s?%s" % (uri, query_str)

        json_bytes = encode_canonical_json(json_body)

        h = self._build_headers(
            headers,
            content_type="application/json",
            accept="application/json",
        )
        response = await self.request("PUT", uri, data=json_bytes, headers=h)
        body = await response.read()

        if 200 <= response.status < 300:
            return json_decoder.loads(body)
        else:
            raise HttpResponseException(
                response.status,
                response.reason or "",
                body,
            )

    async def get_raw(
        self,
        uri: str,
        args: QueryParams | None = None,
        headers: RawHeaders | None = None,
    ) -> bytes:
        """GET raw bytes from a URI.

        Args:
            uri: The URI to request.
            args: Query string parameters.
            headers: Additional headers.

        Returns:
            Response body as bytes.

        Raises:
            HttpResponseException: On non-2xx response.
        """
        if args:
            query_str = urllib.parse.urlencode(args, True)
            uri = "%s?%s" % (uri, query_str)

        h = self._build_headers(headers)
        response = await self.request("GET", uri, headers=h)
        body = await response.read()

        if 200 <= response.status < 300:
            return body
        else:
            raise HttpResponseException(
                response.status,
                response.reason or "",
                body,
            )

    async def get_file(
        self,
        url: str,
        output_stream: BinaryIO,
        max_size: int | None = None,
        headers: RawHeaders | None = None,
        is_allowed_content_type: Callable[[str], bool] | None = None,
    ) -> tuple[int, dict[str, list[str]], str, int]:
        """Download a file from a URL, streaming to output_stream.

        Args:
            url: The URL to download.
            output_stream: File-like object to write response body to.
            max_size: Maximum allowed response body size in bytes.
            headers: Additional headers.
            is_allowed_content_type: Predicate to check content type.

        Returns:
            Tuple of (length, response_headers, final_url, status_code).

        Raises:
            SynapseError: On non-2xx response, oversized body, or timeout.
        """
        h = self._build_headers(headers)
        response = await self.request("GET", url, headers=h)

        resp_headers: dict[str, list[str]] = {}
        for key, value in response.headers.items():
            resp_headers.setdefault(key, []).append(value)

        if response.status > 299:
            logger.warning("Got %d when downloading %s", response.status, url)
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                "Got error %d" % (response.status,),
                Codes.UNKNOWN,
            )

        if is_allowed_content_type and "Content-Type" in resp_headers:
            content_type = resp_headers["Content-Type"][0]
            if not is_allowed_content_type(content_type):
                raise SynapseError(
                    HTTPStatus.BAD_GATEWAY,
                    "Requested file's content type not allowed: %s"
                    % content_type,
                )

        try:
            length = 0
            async with asyncio.timeout(_DEFAULT_DOWNLOAD_TIMEOUT):  # type: ignore[attr-defined]
                async for chunk in response.content.iter_any():
                    length += len(chunk)
                    if max_size is not None and length > max_size:
                        raise SynapseError(
                            HTTPStatus.BAD_GATEWAY,
                            "Requested file is too large > %r bytes"
                            % (max_size,),
                            Codes.TOO_LARGE,
                        )
                    output_stream.write(chunk)
        except TimeoutError:
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                "Requested file took too long to download",
                Codes.TOO_LARGE,
            )
        except SynapseError:
            raise
        except Exception as e:
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                "Failed to download remote body: %s" % e,
            ) from e

        return (
            length,
            resp_headers,
            str(response.url),
            response.status,
        )

    def _build_headers(
        self,
        extra: RawHeaders | None = None,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> dict[str, str]:
        """Build a headers dict from optional extras and standard headers."""
        h: dict[str, str] = {}
        if content_type:
            h["Content-Type"] = content_type
        if accept:
            h["Accept"] = accept
        if extra:
            for raw_key, raw_values in extra.items():
                # RawHeaders is dict[bytes, list[bytes]] — convert to str
                key_str: str = raw_key.decode("ascii") if isinstance(raw_key, bytes) else str(raw_key)
                for v in raw_values:
                    h[key_str] = v.decode("ascii") if isinstance(v, bytes) else str(v)
        return h
