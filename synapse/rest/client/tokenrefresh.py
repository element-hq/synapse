#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.api.errors import AuthError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer


class TokenRefreshRestServlet(RestServlet):
    """
    Exchanges refresh tokens for a pair of an access token and a new refresh
    token.
    """

    PATTERNS = client_patterns("/tokenrefresh")

    def __init__(self, hs: "HomeServer"):
        super().__init__()

    async def on_POST(self, request: Request) -> None:
        raise AuthError(403, "tokenrefresh is no longer supported.")


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    TokenRefreshRestServlet(hs).register(http_server)
