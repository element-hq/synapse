#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from http import HTTPStatus
from typing import TYPE_CHECKING, Optional, Tuple

from synapse.api.constants import EventTypes
from synapse.api.errors import NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import set_tag
from synapse.rest.admin._base import admin_patterns, assert_user_is_admin
from synapse.rest.client.transactions import HttpTransactionCache
from synapse.types import JsonDict, Requester, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


class SendServerNoticeServlet(RestServlet):
    """Servlet which will send a server notice to a given user

    POST /_synapse/admin/v1/send_server_notice
    {
        "user_id": "@target_user:server_name",
        "content": {
            "msgtype": "m.text",
            "body": "This is my message"
        }
    }

    returns:

    {
        "event_id": "$1895723857jgskldgujpious"
    }
    """

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.server_notices_manager = hs.get_server_notices_manager()
        self.admin_handler = hs.get_admin_handler()
        self.txns = HttpTransactionCache(hs)
        self.is_mine = hs.is_mine

    def register(self, json_resource: HttpServer) -> None:
        PATTERN = "/send_server_notice"
        json_resource.register_paths(
            "POST", admin_patterns(PATTERN + "$"), self.on_POST, self.__class__.__name__
        )
        json_resource.register_paths(
            "PUT",
            admin_patterns(PATTERN + "/(?P<txn_id>[^/]*)$"),
            self.on_PUT,
            self.__class__.__name__,
        )

    async def _do(
        self,
        request: SynapseRequest,
        requester: Requester,
        txn_id: Optional[str],
    ) -> Tuple[int, JsonDict]:
        await assert_user_is_admin(self.auth, requester)
        body = parse_json_object_from_request(request)
        assert_params_in_dict(body, ("user_id", "content"))
        event_type = body.get("type", EventTypes.Message)
        state_key = body.get("state_key")

        # We grab the server notices manager here as its initialisation has a check for worker processes,
        # but worker processes still need to initialise SendServerNoticeServlet (as it is part of the
        # admin api).
        if not self.server_notices_manager.is_enabled():
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Server notices are not enabled on this server"
            )

        target_user = UserID.from_string(body["user_id"])
        if not self.is_mine(target_user):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Server notices can only be sent to local users"
            )

        if not await self.admin_handler.get_user(target_user):
            raise NotFoundError("User not found")

        event = await self.server_notices_manager.send_notice(
            user_id=target_user.to_string(),
            type=event_type,
            state_key=state_key,
            event_content=body["content"],
            txn_id=txn_id,
        )

        return HTTPStatus.OK, {"event_id": event.event_id}

    async def on_POST(
        self,
        request: SynapseRequest,
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        return await self._do(request, requester, None)

    async def on_PUT(
        self, request: SynapseRequest, txn_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        set_tag("txn_id", txn_id)
        return await self.txns.fetch_or_execute_request(
            request, requester, self._do, request, requester, txn_id
        )
