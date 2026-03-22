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

import abc
import base64
import logging
from typing import Any

import attr

logger = logging.getLogger(__name__)


class ProxyConnectError(ConnectionError):
    pass


class ProxyCredentials:
    @abc.abstractmethod
    def as_proxy_authorization_value(self) -> bytes:
        raise NotImplementedError()


@attr.s(auto_attribs=True)
class BasicProxyCredentials(ProxyCredentials):
    username_password: bytes

    def as_proxy_authorization_value(self) -> bytes:
        """
        Return the value for a Proxy-Authorization header (i.e. 'Basic abdef==').

        Returns:
            A transformation of the authentication string the encoded value for
            a Proxy-Authorization header.
        """
        # Encode as base64 and prepend the authorization type
        return b"Basic " + base64.b64encode(self.username_password)


@attr.s(auto_attribs=True)
class BearerProxyCredentials(ProxyCredentials):
    access_token: bytes

    def as_proxy_authorization_value(self) -> bytes:
        """
        Return the value for a Proxy-Authorization header (i.e. 'Bearer xxx').
        """
        return b"Bearer " + self.access_token


# The Twisted-based HTTP CONNECT proxy protocol classes (HTTPConnectProxyEndpoint,
# HTTPProxiedClientFactory, HTTPConnectProtocol, HTTPConnectSetupClient) have been
# removed as part of the Twisted->asyncio migration. The proxy functionality is now
# handled by aiohttp's native proxy support in NativeSimpleHttpClient.
#
# For backward compatibility, we keep a stub that raises if someone tries to use it.
class HTTPConnectProxyEndpoint:
    """Stub: The Twisted HTTP CONNECT proxy endpoint has been removed.

    Proxy support is now handled by aiohttp natively.
    """
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "HTTPConnectProxyEndpoint has been removed. "
            "Use aiohttp's native proxy support instead."
        )
