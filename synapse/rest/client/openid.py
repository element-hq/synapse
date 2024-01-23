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

import logging
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import AuthError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict
from synapse.util.stringutils import random_string

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class IdTokenServlet(RestServlet):
    """
    Get a bearer token that may be passed to a third party to confirm ownership
    of a matrix user id.

    The format of the response could be made compatible with the format given
    in http://openid.net/specs/openid-connect-core-1_0.html#TokenResponse

    But instead of returning a signed "id_token" the response contains the
    name of the issuing matrix homeserver. This means that for now the third
    party will need to check the validity of the "id_token" against the
    federation /openid/userinfo endpoint of the homeserver.

    Request:

    POST /user/{user_id}/openid/request_token?access_token=... HTTP/1.1

    {}

    Response:

    HTTP/1.1 200 OK
    {
        "access_token": "ABDEFGH",
        "token_type": "Bearer",
        "matrix_server_name": "example.com",
        "expires_in": 3600,
    }
    """

    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/openid/request_token")

    EXPIRES_MS = 3600 * 1000

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()
        self.server_name = hs.config.server.server_name

    async def on_POST(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot request tokens for other users.")

        # Parse the request body to make sure it's JSON, but ignore the contents
        # for now.
        parse_json_object_from_request(request)

        token = random_string(24)
        ts_valid_until_ms = self.clock.time_msec() + self.EXPIRES_MS

        await self.store.insert_open_id_token(token, ts_valid_until_ms, user_id)

        return (
            200,
            {
                "access_token": token,
                "token_type": "Bearer",
                "matrix_server_name": self.server_name,
                "expires_in": self.EXPIRES_MS // 1000,
            },
        )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    IdTokenServlet(hs).register(http_server)
