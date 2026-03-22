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

from netaddr import AddrFormatError, IPAddress

from synapse.http.federation.srv_resolver import Server, SrvResolver

logger = logging.getLogger(__name__)


def _is_ip_literal(host: bytes) -> bool:
    """Test if the given host name is either an IPv4 or IPv6 literal.

    Args:
        host: The host name to check

    Returns:
        True if the hostname is an IP address literal.
    """

    host_str = host.decode("ascii")

    try:
        IPAddress(host_str)
        return True
    except AddrFormatError:
        return False


# The Twisted-based MatrixFederationAgent, MatrixHostnameEndpointFactory, and
# MatrixHostnameEndpoint classes have been removed as part of the Twisted->asyncio
# migration. Federation HTTP requests are now handled directly by
# MatrixFederationHttpClient using aiohttp.
#
# For backward compatibility of imports, we provide a stub.
class MatrixFederationAgent:
    """Stub: The Twisted MatrixFederationAgent has been removed.

    Federation requests are now handled by MatrixFederationHttpClient using aiohttp.
    """
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "MatrixFederationAgent has been removed. "
            "Federation requests use aiohttp directly via MatrixFederationHttpClient."
        )
