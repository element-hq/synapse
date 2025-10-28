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

from synapse.api.constants import AccountDataTypes, ReceiptTypes
from synapse.api.errors import AuthError, Codes, NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict, JsonMapping, RoomID

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


def _check_can_set_account_data_type(account_data_type: str) -> None:
    """The fully read marker and push rules cannot be directly set via /account_data."""
    if account_data_type == ReceiptTypes.FULLY_READ:
        raise SynapseError(
            405,
            "Cannot set m.fully_read through this API."
            " Use /rooms/!roomId:server.name/read_markers",
            Codes.BAD_JSON,
        )
    elif account_data_type == AccountDataTypes.PUSH_RULES:
        raise SynapseError(
            405,
            "Cannot set m.push_rules through this API. Use /pushrules",
            Codes.BAD_JSON,
        )


class AccountDataServlet(RestServlet):
    """
    PUT /user/{user_id}/account_data/{account_dataType} HTTP/1.1
    GET /user/{user_id}/account_data/{account_dataType} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)/account_data/(?P<account_data_type>[^/]*)"
    )
    CATEGORY = "Account data requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.handler = hs.get_account_data_handler()
        self._push_rules_handler = hs.get_push_rules_handler()

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, account_data_type: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot add account data for other users.")

        # Raise an error if the account data type cannot be set directly.
        _check_can_set_account_data_type(account_data_type)

        body = parse_json_object_from_request(request)

        # If experimental support for MSC3391 is enabled, then providing an empty dict
        # as the value for an account data type should be functionally equivalent to
        # calling the DELETE method on the same type.
        if self._hs.config.experimental.msc3391_enabled:
            if body == {}:
                await self.handler.remove_account_data_for_user(
                    user_id, account_data_type
                )
                return 200, {}

        await self.handler.add_account_data_for_user(user_id, account_data_type, body)

        return 200, {}

    async def on_GET(
        self, request: SynapseRequest, user_id: str, account_data_type: str
    ) -> tuple[int, JsonMapping]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot get account data for other users.")

        # Push rules are stored in a separate table and must be queried separately.
        if account_data_type == AccountDataTypes.PUSH_RULES:
            account_data: (
                JsonMapping | None
            ) = await self._push_rules_handler.push_rules_for_user(requester.user)
        else:
            account_data = await self.store.get_global_account_data_by_type_for_user(
                user_id, account_data_type
            )

        if account_data is None:
            raise NotFoundError("Account data not found")

        # If experimental support for MSC3391 is enabled, then this endpoint should
        # return a 404 if the content for an account data type is an empty dict.
        if self._hs.config.experimental.msc3391_enabled and account_data == {}:
            raise NotFoundError("Account data not found")

        return 200, account_data


class UnstableAccountDataServlet(RestServlet):
    """
    Contains an unstable endpoint for removing user account data, as specified by
    MSC3391. If that MSC is accepted, this code should have unstable prefixes removed
    and become incorporated into AccountDataServlet above.
    """

    PATTERNS = client_patterns(
        "/org.matrix.msc3391/user/(?P<user_id>[^/]*)"
        "/account_data/(?P<account_data_type>[^/]*)",
        unstable=True,
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self.auth = hs.get_auth()
        self.handler = hs.get_account_data_handler()

    async def on_DELETE(
        self,
        request: SynapseRequest,
        user_id: str,
        account_data_type: str,
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot delete account data for other users.")

        # Raise an error if the account data type cannot be set directly.
        _check_can_set_account_data_type(account_data_type)

        await self.handler.remove_account_data_for_user(user_id, account_data_type)

        return 200, {}


class RoomAccountDataServlet(RestServlet):
    """
    PUT /user/{user_id}/rooms/{room_id}/account_data/{account_dataType} HTTP/1.1
    GET /user/{user_id}/rooms/{room_id}/account_data/{account_dataType} HTTP/1.1
    """

    PATTERNS = client_patterns(
        "/user/(?P<user_id>[^/]*)"
        "/rooms/(?P<room_id>[^/]*)"
        "/account_data/(?P<account_data_type>[^/]*)"
    )
    CATEGORY = "Account data requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.handler = hs.get_account_data_handler()

    async def on_PUT(
        self,
        request: SynapseRequest,
        user_id: str,
        room_id: str,
        account_data_type: str,
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot add account data for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        # Raise an error if the account data type cannot be set directly.
        _check_can_set_account_data_type(account_data_type)

        body = parse_json_object_from_request(request)

        # If experimental support for MSC3391 is enabled, then providing an empty dict
        # as the value for an account data type should be functionally equivalent to
        # calling the DELETE method on the same type.
        if self._hs.config.experimental.msc3391_enabled:
            if body == {}:
                await self.handler.remove_account_data_for_room(
                    user_id, room_id, account_data_type
                )
                return 200, {}

        await self.handler.add_account_data_to_room(
            user_id, room_id, account_data_type, body
        )

        return 200, {}

    async def on_GET(
        self,
        request: SynapseRequest,
        user_id: str,
        room_id: str,
        account_data_type: str,
    ) -> tuple[int, JsonMapping]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot get account data for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        # Room-specific push rules are not currently supported.
        if account_data_type == AccountDataTypes.PUSH_RULES:
            account_data: JsonMapping | None = {}
        else:
            account_data = await self.store.get_account_data_for_room_and_type(
                user_id, room_id, account_data_type
            )

        if account_data is None:
            raise NotFoundError("Room account data not found")

        # If experimental support for MSC3391 is enabled, then this endpoint should
        # return a 404 if the content for an account data type is an empty dict.
        if self._hs.config.experimental.msc3391_enabled and account_data == {}:
            raise NotFoundError("Room account data not found")

        return 200, account_data


class UnstableRoomAccountDataServlet(RestServlet):
    """
    Contains an unstable endpoint for removing room account data, as specified by
    MSC3391. If that MSC is accepted, this code should have unstable prefixes removed
    and become incorporated into RoomAccountDataServlet above.
    """

    PATTERNS = client_patterns(
        "/org.matrix.msc3391/user/(?P<user_id>[^/]*)"
        "/rooms/(?P<room_id>[^/]*)"
        "/account_data/(?P<account_data_type>[^/]*)",
        unstable=True,
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self._hs = hs
        self.auth = hs.get_auth()
        self.handler = hs.get_account_data_handler()

    async def on_DELETE(
        self,
        request: SynapseRequest,
        user_id: str,
        room_id: str,
        account_data_type: str,
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        if user_id != requester.user.to_string():
            raise AuthError(403, "Cannot delete account data for other users.")

        if not RoomID.is_valid(room_id):
            raise SynapseError(
                400,
                f"{room_id} is not a valid room ID",
                Codes.INVALID_PARAM,
            )

        # Raise an error if the account data type cannot be set directly.
        _check_can_set_account_data_type(account_data_type)

        await self.handler.remove_account_data_for_room(
            user_id, room_id, account_data_type
        )

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    AccountDataServlet(hs).register(http_server)
    RoomAccountDataServlet(hs).register(http_server)

    if hs.config.experimental.msc3391_enabled:
        UnstableAccountDataServlet(hs).register(http_server)
        UnstableRoomAccountDataServlet(hs).register(http_server)
