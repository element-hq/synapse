#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2023 The Matrix.org Foundation C.I.C.
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
from typing import Any

logger = logging.getLogger(__name__)

# "Hop-by-hop" headers (as opposed to "end-to-end" headers) as defined by RFC2616
# section 13.5.1 and referenced in RFC9110 section 7.6.1. These are meant to only be
# consumed by the immediate recipient and not be forwarded on.
HOP_BY_HOP_HEADERS_LOWERCASE = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
assert all(header.lower() == header for header in HOP_BY_HOP_HEADERS_LOWERCASE)


def parse_connection_header_value(
    connection_header_value: bytes | None,
) -> set[str]:
    """
    Parse the `Connection` header to determine which headers we should not be copied
    over from the remote response.

    As defined by RFC2616 section 14.10 and RFC9110 section 7.6.1

    Example: `Connection: close, X-Foo, X-Bar` will return `{"Close", "X-Foo", "X-Bar"}`

    Even though "close" is a special directive, let's just treat it as just another
    header for simplicity. If people want to check for this directive, they can simply
    check for `"Close" in headers`.

    Args:
        connection_header_value: The value of the `Connection` header.

    Returns:
        The set of header names that should not be copied over from the remote response.
        The keys are lowercased.
    """
    extra_headers_to_remove: set[str] = set()
    if connection_header_value:
        extra_headers_to_remove = {
            connection_option.decode("ascii").strip().lower()
            for connection_option in connection_header_value.split(b",")
        }

    return extra_headers_to_remove


# The Twisted-based ProxyResource, _ProxyResponseBody, and ProxySite classes have been
# removed as part of the Twisted->asyncio migration. Federation proxy functionality
# would need to be reimplemented using aiohttp if needed.
#
# For backward compatibility of imports, we provide stubs.
class ProxyResource:
    """Stub: The Twisted ProxyResource has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ProxyResource has been removed as part of the Twisted->asyncio migration."
        )


class ProxySite:
    """Stub: The Twisted ProxySite has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ProxySite has been removed as part of the Twisted->asyncio migration."
        )
