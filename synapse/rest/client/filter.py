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
from typing import TYPE_CHECKING

from synapse.api.errors import AuthError, NotFoundError, StoreError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict, JsonMapping, UserID

from ._base import client_patterns, set_timeline_upper_limit

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class GetFilterRestServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/filter/(?P<filter_id>[^/]*)")
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.filtering = hs.get_filtering()

    async def on_GET(
        self, request: SynapseRequest, user_id: str, filter_id: str
    ) -> tuple[int, JsonMapping]:
        target_user = UserID.from_string(user_id)
        requester = await self.auth.get_user_by_req(request)

        if target_user != requester.user:
            raise AuthError(403, "Cannot get filters for other users")

        if not self.hs.is_mine(target_user):
            raise AuthError(403, "Can only get filters for local users")

        try:
            filter_id_int = int(filter_id)
        except Exception:
            raise SynapseError(400, "Invalid filter_id")

        try:
            filter_collection = await self.filtering.get_user_filter(
                user_id=target_user, filter_id=filter_id_int
            )
        except StoreError as e:
            if e.code != 404:
                raise
            raise NotFoundError("No such filter")

        return 200, filter_collection.get_filter_json()


class CreateFilterRestServlet(RestServlet):
    PATTERNS = client_patterns("/user/(?P<user_id>[^/]*)/filter")
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.filtering = hs.get_filtering()

    async def on_POST(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, JsonDict]:
        target_user = UserID.from_string(user_id)
        requester = await self.auth.get_user_by_req(request)

        if target_user != requester.user:
            raise AuthError(403, "Cannot create filters for other users")

        if not self.hs.is_mine(target_user):
            raise AuthError(403, "Can only create filters for local users")

        content = parse_json_object_from_request(request)
        set_timeline_upper_limit(content, self.hs.config.server.filter_timeline_limit)

        filter_id = await self.filtering.add_user_filter(
            user_id=target_user, user_filter=content
        )

        return 200, {"filter_id": str(filter_id)}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    GetFilterRestServlet(hs).register(http_server)
    CreateFilterRestServlet(hs).register(http_server)
