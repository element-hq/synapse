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

from typing import TYPE_CHECKING

from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_boolean
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.streams.config import PaginationConfig
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


# TODO: Needs unit testing
class InitialSyncRestServlet(RestServlet):
    PATTERNS = client_patterns("/initialSync$", v1=True)
    CATEGORY = "Sync requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.initial_sync_handler = hs.get_initial_sync_handler()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main

    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        args: dict[bytes, list[bytes]] = request.args  # type: ignore
        as_client_event = b"raw" not in args
        pagination_config = await PaginationConfig.from_request(
            self.store, request, default_limit=10
        )
        include_archived = parse_boolean(request, "archived", default=False)
        content = await self.initial_sync_handler.snapshot_all_rooms(
            user_id=requester.user.to_string(),
            pagin_config=pagination_config,
            as_client_event=as_client_event,
            include_archived=include_archived,
        )

        return 200, content


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    InitialSyncRestServlet(hs).register(http_server)
