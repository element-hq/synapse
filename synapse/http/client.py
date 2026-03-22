import asyncio
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
from typing import (
    TYPE_CHECKING,
    Any,
    BinaryIO,
    Callable,
    Mapping,
    Optional,
    Protocol,
)

import aiohttp
import attr
from netaddr import AddrFormatError, IPAddress, IPSet
from prometheus_client import Counter

from synapse.api.errors import Codes, HttpResponseException, SynapseError
from synapse.http import RequestTimedOutError, redact_uri
from synapse.http.types import QueryParams
from synapse.logging.opentracing import set_tag, start_active_span, tags
from synapse.metrics import SERVER_NAME_LABEL
from synapse.types import ISynapseReactor, StrSequence
from synapse.util.clock import Clock

if TYPE_CHECKING:
    from synapse.server import HomeServer

# Support both import names for the `python-multipart` (PyPI) library.
try:
    from python_multipart import MultipartParser

    if TYPE_CHECKING:
        from python_multipart import multipart
except ImportError:
    from multipart import MultipartParser  # type: ignore[no-redef]


logger = logging.getLogger(__name__)

outgoing_requests_counter = Counter(
    "synapse_http_client_requests", "", labelnames=["method", SERVER_NAME_LABEL]
)
incoming_responses_counter = Counter(
    "synapse_http_client_responses",
    "",
    labelnames=["method", "code", SERVER_NAME_LABEL],
)

# the type of the headers map, to be passed to the t.w.h.Headers.
#
# The actual type accepted by Twisted is
#   Mapping[str | bytes], Sequence[str | bytes] ,
# allowing us to mix and match str and bytes freely. However: any str is also a
# Sequence[str]; passing a header string value which is a
# standalone str is interpreted as a sequence of 1-codepoint strings. This is a disastrous footgun.
# We use a narrower value type (RawHeaderValue) to avoid this footgun.
#
# We also simplify the keys to be either all str or all bytes. This helps because
# Dict[K, V] is invariant in K (and indeed V).
RawHeaders = Mapping[str, "RawHeaderValue"] | Mapping[bytes, "RawHeaderValue"]

# the value actually has to be a List, but List is invariant so we can't specify that
# the entries can either be Lists or bytes.
RawHeaderValue = (
    StrSequence
    | list[bytes]
    | list[str | bytes]
    | tuple[bytes, ...]
    | tuple[str | bytes, ...]
)


def _is_ip_blocked(
    ip_address: IPAddress, allowlist: IPSet | None, blocklist: IPSet
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


class BlocklistingReactorWrapper:
    """
    A wrapper which filters out blocked IP addresses from DNS resolution,
    to prevent DNS rebinding.

    Kept for backward compatibility; the actual IP blocking for aiohttp-based
    clients is done via aiohttp's TCPConnector with a custom resolver.
    """

    def __init__(
        self,
        reactor: Any,
        ip_allowlist: IPSet | None,
        ip_blocklist: IPSet,
    ):
        self._reactor = reactor
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._reactor, attr)


class BlocklistingAgentWrapper:
    """
    Stub for backward compatibility. The actual IP blocking for aiohttp is done
    via aiohttp's connector.
    """

    def __init__(
        self,
        agent: Any,
        ip_blocklist: IPSet,
        ip_allowlist: IPSet | None = None,
    ):
        self._agent = agent
        self._ip_allowlist = ip_allowlist
        self._ip_blocklist = ip_blocklist

    def request(self, *args: Any, **kwargs: Any) -> Any:
        return self._agent.request(*args, **kwargs)


class ByteWriteable(Protocol):
    """The type of object which must be passed into read_body_with_max_size.

    Typically this is a file object.
    """

    def write(self, data: bytes) -> int:
        pass


class BodyExceededMaxSize(Exception):
    """The maximum allowed size of the HTTP body was exceeded."""


@attr.s(auto_attribs=True, slots=True)
class MultipartResponse:
    """
    A small class to hold parsed values of a multipart response.
    """

    json: bytes = b"{}"
    length: int | None = None
    content_type: bytes | None = None
    disposition: bytes | None = None
    url: bytes | None = None


async def async_read_body_with_max_size(
    response: aiohttp.ClientResponse,
    stream: ByteWriteable,
    max_size: int | None,
) -> int:
    """Read an aiohttp response body, streaming to `stream`, with size limit.

    Args:
        response: The aiohttp response to read from.
        stream: The file-object to write to.
        max_size: The maximum file size to allow, or None for no limit.

    Returns:
        The length of the read body.

    Raises:
        BodyExceededMaxSize: if the body exceeds the max size.
    """
    length = 0
    if max_size is not None and response.content_length is not None:
        if response.content_length > max_size:
            raise BodyExceededMaxSize()
    async for chunk in response.content.iter_any():
        length += len(chunk)
        if max_size is not None and length > max_size:
            raise BodyExceededMaxSize()
        stream.write(chunk)
    return length


def encode_query_args(args: QueryParams | None) -> bytes:
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


def is_unknown_endpoint(
    e: HttpResponseException, synapse_error: SynapseError | None = None
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


def check_content_type_is(headers: dict[bytes, list[bytes]], expected_content_type: str) -> None:
    """Check that the Content-Type header matches the expected value.

    Args:
        headers: response headers
        expected_content_type: expected content type

    Raises:
        RequestSendFailed if the content type doesn't match.
    """
    content_type_headers = headers.get(b"Content-Type", headers.get(b"content-type", []))
    if not content_type_headers:
        raise RuntimeError(f"No Content-Type header, expected {expected_content_type}")

    c_type = content_type_headers[0].decode("ascii")
    # Check the main type, ignoring parameters
    main_type = c_type.split(";", 1)[0].strip()
    if main_type != expected_content_type:
        raise RuntimeError(
            f"Content-Type was {c_type}, expected {expected_content_type}"
        )


def check_content_type_is_aiohttp(
    response: aiohttp.ClientResponse, expected_content_type: str
) -> None:
    """Check that an aiohttp response has the expected Content-Type.

    Args:
        response: The aiohttp response.
        expected_content_type: The expected content type.

    Raises:
        RuntimeError if the content type doesn't match.
    """
    c_type = response.content_type or ""
    if c_type != expected_content_type:
        raise RuntimeError(
            f"Content-Type was {c_type}, expected {expected_content_type}"
        )


# Backward compatibility stubs for Twisted-based HTTP client classes.
# These have been superseded by NativeSimpleHttpClient (aiohttp-based).

class SimpleHttpClient:
    """Stub: The Twisted SimpleHttpClient has been removed.

    Use NativeSimpleHttpClient instead.
    """
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "SimpleHttpClient has been removed. Use NativeSimpleHttpClient instead."
        )


class BaseHttpClient:
    """Stub: The Twisted BaseHttpClient has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "BaseHttpClient has been removed. Use NativeSimpleHttpClient instead."
        )


class ReplicationClient:
    """Stub: The Twisted ReplicationClient has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ReplicationClient has been removed. Use NativeReplicationClient instead."
        )


class InsecureInterceptableContextFactory:
    """Stub: The Twisted InsecureInterceptableContextFactory has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "InsecureInterceptableContextFactory has been removed."
        )


# Keep these for backward compatibility of imports that use Twisted-era functions.
# They delegate to Twisted if available, otherwise raise.

def read_body_with_max_size(*args: Any, **kwargs: Any) -> Any:
    """Stub: requires Twisted. Use async_read_body_with_max_size for aiohttp."""
    try:
        from twisted.internet import defer, protocol
        # If Twisted is available, provide the original implementation
        raise NotImplementedError("Use async_read_body_with_max_size instead")
    except ImportError:
        raise NotImplementedError(
            "read_body_with_max_size requires Twisted. "
            "Use async_read_body_with_max_size for aiohttp responses."
        )


def read_multipart_response(*args: Any, **kwargs: Any) -> Any:
    """Stub: requires Twisted. Use aiohttp multipart parsing instead."""
    raise NotImplementedError(
        "read_multipart_response requires Twisted. "
        "Use aiohttp multipart parsing instead."
    )
