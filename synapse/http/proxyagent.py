#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
import re
from typing import Any, Optional
from urllib.parse import urlparse

from synapse.http.connectproxyclient import (
    BasicProxyCredentials,
    ProxyCredentials,
)

logger = logging.getLogger(__name__)

_VALID_URI = re.compile(rb"\A[\x21-\x7e]+\Z")


def http_proxy_endpoint(
    proxy: bytes | None,
    reactor: Any = None,
    tls_options_factory: Any = None,
    timeout: float = 30,
    bindAddress: Any = None,
    attemptDelay: float | None = None,
) -> tuple[Any, ProxyCredentials | None]:
    """Parses an http proxy setting and returns endpoint info for the proxy.

    This is a compatibility shim. The Twisted endpoint-based proxy support has been
    removed. This function now only parses the proxy URL and returns credentials.

    Args:
        proxy: the proxy setting in the form: [scheme://][<username>:<password>@]<host>[:<port>]
        reactor: unused (kept for backward compatibility)
        tls_options_factory: unused (kept for backward compatibility)
        kwargs: other args (unused)

    Returns:
        A tuple of (None, ProxyCredentials or None). The first element is always None
        since Twisted endpoints are no longer used.
    """
    if proxy is None:
        return None, None

    scheme, host, port, credentials = parse_proxy(proxy)
    # We return None for the endpoint since aiohttp handles proxying natively.
    # The credentials are still useful for proxy authentication headers.
    return None, credentials


def parse_proxy(
    proxy: bytes, default_scheme: bytes = b"http", default_port: int = 1080
) -> tuple[bytes, bytes, int, ProxyCredentials | None]:
    """
    Parse a proxy connection string.

    Given a HTTP proxy URL, breaks it down into components and checks that it
    has a hostname (otherwise it is not useful to us when trying to find a
    proxy) and asserts that the URL has a scheme we support.

    Args:
        proxy: The proxy connection string. Must be in the form '[scheme://][<username>:<password>@]host[:port]'.
        default_scheme: The default scheme to return if one is not found in `proxy`. Defaults to http
        default_port: The default port to return if one is not found in `proxy`. Defaults to 1080

    Returns:
        A tuple containing the scheme, hostname, port and ProxyCredentials.
            If no credentials were found, the ProxyCredentials instance is replaced with None.

    Raise:
        ValueError if proxy has no hostname or unsupported scheme.
    """
    # First check if we have a scheme present
    # Note: urlsplit/urlparse cannot be used (for Python # 3.9+) on scheme-less proxies, e.g. host:port.
    if b"://" not in proxy:
        proxy = b"".join([default_scheme, b"://", proxy])

    url = urlparse(proxy)

    if not url.hostname:
        raise ValueError("Proxy URL did not contain a hostname! Please specify one.")

    if url.scheme not in (b"http", b"https"):
        raise ValueError(
            f"Unknown proxy scheme {url.scheme!s}; only 'http' and 'https' is supported."
        )

    credentials = None
    if url.username and url.password:
        credentials = BasicProxyCredentials(
            b"".join([url.username, b":", url.password])
        )

    return url.scheme, url.hostname, url.port or default_port, credentials


# The Twisted-based ProxyAgent class has been removed as part of the Twisted->asyncio
# migration. Proxy support is now handled by aiohttp's native proxy support in
# NativeSimpleHttpClient.
#
# For backward compatibility of imports, we provide a stub.
class ProxyAgent:
    """Stub: The Twisted ProxyAgent has been removed.

    Proxy support is now handled by aiohttp natively in NativeSimpleHttpClient.
    """
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ProxyAgent has been removed. Use NativeSimpleHttpClient with "
            "aiohttp's native proxy support instead."
        )
