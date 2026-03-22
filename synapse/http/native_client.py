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

Provides NativeSimpleHttpClient as a replacement for SimpleHttpClient, using
aiohttp.ClientSession instead of treq + Twisted Agent.
"""

import asyncio
import logging
import ssl
import urllib.parse
from http import HTTPStatus
from io import BytesIO
from typing import (
    TYPE_CHECKING,
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

if TYPE_CHECKING:
    from synapse.server import HomeServer

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
        hs_or_user_agent: "HomeServer | str",
        ip_allowlist: IPSet | None = None,
        ip_blocklist: IPSet | None = None,
        use_proxy: bool = False,
        # Explicit params (used when hs_or_user_agent is a string)
        proxy_url: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_connections: int = 100,
        connection_timeout: float = 15.0,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        # Extra kwargs accepted for BaseHttpClient compat (ignored)
        **kwargs: Any,
    ) -> None:
        """
        Can be constructed with either a HomeServer or explicit params.

        HomeServer mode (matches SimpleHttpClient API):
            NativeSimpleHttpClient(hs, ip_allowlist=..., ip_blocklist=..., use_proxy=...)

        Explicit mode:
            NativeSimpleHttpClient(user_agent, ip_blocklist=..., proxy_url=..., ...)
        """
        if isinstance(hs_or_user_agent, str):
            # Explicit mode
            user_agent = hs_or_user_agent
        else:
            # HomeServer mode
            hs = hs_or_user_agent
            ua = hs.version_string
            user_agent = ua.decode("ascii") if isinstance(ua, bytes) else ua
            max_connections = max(int(100 * hs.config.caches.global_factor), 5)
            if use_proxy:
                proxy_config = hs.config.server.proxy_config
                if proxy_config:
                    # Prefer HTTPS proxy, fall back to HTTP proxy
                    proxy_url = proxy_config.https_proxy or proxy_config.http_proxy

        self.user_agent = user_agent
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist or IPSet()
        self._proxy_url = proxy_url
        self._request_timeout = request_timeout
        self._ssl_context = ssl_context
        self._max_connections = max_connections
        self._connection_timeout = connection_timeout
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session on first use."""
        if self._session is None:
            resolver = None
            if self._ip_blocklist:
                resolver = _BlocklistingResolver(self._ip_allowlist, self._ip_blocklist)

            connector = aiohttp.TCPConnector(
                limit_per_host=self._max_connections,
                resolver=resolver,
                ssl=self._ssl_context or False,
            )

            timeout = aiohttp.ClientTimeout(
                sock_connect=self._connection_timeout,
                total=None,  # We manage total timeout per-request
            )

            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": self.user_agent},
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session is not None:
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
            session = self._get_session()
            response = await asyncio.wait_for(
                session.request(
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
    ) -> tuple[int, dict[bytes, list[bytes]], str, int]:
        """Download a file from a URL, streaming to output_stream.

        Args:
            url: The URL to download.
            output_stream: File-like object to write response body to.
            max_size: Maximum allowed response body size in bytes.
            headers: Additional headers.
            is_allowed_content_type: Predicate to check content type.

        Returns:
            Tuple of (length, response_headers, final_url, status_code).
            Headers are bytes-keyed for compatibility with existing callers.

        Raises:
            SynapseError: On non-2xx response, oversized body, or timeout.
        """
        h = self._build_headers(headers)
        response = await self.request("GET", url, headers=h)

        # Build bytes-keyed headers for backward compatibility
        resp_headers: dict[bytes, list[bytes]] = {}
        for key, value in response.headers.items():
            resp_headers.setdefault(key.encode("ascii"), []).append(value.encode("utf-8"))

        if response.status > 299:
            logger.warning("Got %d when downloading %s", response.status, url)
            raise SynapseError(
                HTTPStatus.BAD_GATEWAY,
                "Got error %d" % (response.status,),
                Codes.UNKNOWN,
            )

        if is_allowed_content_type and b"Content-Type" in resp_headers:
            content_type = resp_headers[b"Content-Type"][0].decode("ascii")
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


class NativeReplicationClient(NativeSimpleHttpClient):
    """asyncio-native replication HTTP client.

    Routes requests with the `synapse-replication://` scheme to the appropriate
    worker instance (TCP or UNIX socket) based on the instance_map config.
    """

    def __init__(self, hs: "HomeServer") -> None:
        super().__init__(hs)
        self.server_name = hs.hostname
        self._instance_map = hs.config.worker.instance_map

    def _get_session(self) -> aiohttp.ClientSession:
        """Lazily create the aiohttp session for replication."""
        if self._session is None:
            connector = aiohttp.TCPConnector(
                limit_per_host=5,
                ssl=False,
            )
            timeout = aiohttp.ClientTimeout(
                sock_connect=15,
                total=None,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": self.user_agent},
            )
        return self._session

    async def request(
        self,
        method: str,
        uri: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> aiohttp.ClientResponse:
        """Make a replication request.

        Translates synapse-replication:// URIs to actual worker endpoints.
        """
        from synapse.config.workers import (
            InstanceTcpLocationConfig,
            InstanceUnixLocationConfig,
        )
        from synapse.http.client import RequestTimedOutError

        outgoing_requests_counter.labels(method=method).inc()
        logger.debug("Sending replication request %s %s", method, uri)

        # Parse the synapse-replication:// URI to get the instance name
        parsed = urllib.parse.urlparse(uri)
        instance_name = parsed.hostname or ""
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"

        # Look up the instance in the config
        location = self._instance_map.get(instance_name)
        if location is None:
            raise Exception(f"Instance {instance_name!r} not in instance_map config")

        # Build the actual URL
        if isinstance(location, InstanceTcpLocationConfig):
            scheme = "https" if location.tls else "http"
            actual_url = f"{scheme}://{location.host}:{location.port}{path}"
        elif isinstance(location, InstanceUnixLocationConfig):
            # aiohttp supports UNIX sockets via a UnixConnector, but we need
            # a separate session for that. For simplicity, use the HTTP URL
            # format that aiohttp's UnixConnector expects.
            actual_url = f"http://localhost{path}"
        else:
            raise Exception(f"Unknown location type for {instance_name}: {type(location)}")

        try:
            # For UNIX sockets, we need a different connector
            if isinstance(location, InstanceUnixLocationConfig):
                connector = aiohttp.UnixConnector(path=location.path)
                async with aiohttp.ClientSession(
                    connector=connector,
                    headers={"User-Agent": self.user_agent},
                ) as session:
                    response = await asyncio.wait_for(
                        session.request(method, actual_url, data=data, headers=headers),
                        timeout=60,
                    )
            else:
                session = self._get_session()
                response = await asyncio.wait_for(
                    session.request(method, actual_url, data=data, headers=headers),
                    timeout=60,
                )

            incoming_responses_counter.labels(
                method=method, code=response.status
            ).inc()
            logger.info(
                "Received replication response to %s %s: %s",
                method, uri, response.status,
            )
            return response

        except asyncio.TimeoutError:
            incoming_responses_counter.labels(method=method, code="ERR").inc()
            logger.warning("Timeout on replication request %s %s", method, uri)
            raise RequestTimedOutError(None)
        except Exception as e:
            incoming_responses_counter.labels(method=method, code="ERR").inc()
            logger.info(
                "Error on replication request %s %s: %s %s",
                method, uri, type(e).__name__, e,
            )
            raise


# Alias for drop-in replacement of synapse.http.client.SimpleHttpClient
SimpleHttpClient = NativeSimpleHttpClient
