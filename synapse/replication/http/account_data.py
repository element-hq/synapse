#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationAddUserAccountDataRestServlet(ReplicationEndpoint):
    """Add user account data on the appropriate account data worker.

    Request format:

        POST /_synapse/replication/add_user_account_data/:user_id/:type

        {
            "content": { ... },
        }

    """

    NAME = "add_user_account_data"
    PATH_ARGS = ("user_id", "account_data_type")
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_account_data_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, account_data_type: str, content: JsonDict
    ) -> JsonDict:
        payload = {
            "content": content,
        }

        return payload

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str, account_data_type: str
    ) -> tuple[int, JsonDict]:
        max_stream_id = await self.handler.add_account_data_for_user(
            user_id, account_data_type, content["content"]
        )

        return 200, {"max_stream_id": max_stream_id}


class ReplicationRemoveUserAccountDataRestServlet(ReplicationEndpoint):
    """Remove user account data on the appropriate account data worker.

    Request format:

        POST /_synapse/replication/remove_user_account_data/:user_id/:type

        {
            "content": { ... },
        }

    """

    NAME = "remove_user_account_data"
    PATH_ARGS = ("user_id", "account_data_type")
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_account_data_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, account_data_type: str
    ) -> JsonDict:
        return {}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str, account_data_type: str
    ) -> tuple[int, JsonDict]:
        max_stream_id = await self.handler.remove_account_data_for_user(
            user_id, account_data_type
        )

        return 200, {"max_stream_id": max_stream_id}


class ReplicationAddRoomAccountDataRestServlet(ReplicationEndpoint):
    """Add room account data on the appropriate account data worker.

    Request format:

        POST /_synapse/replication/add_room_account_data/:user_id/:room_id/:account_data_type

        {
            "content": { ... },
        }

    """

    NAME = "add_room_account_data"
    PATH_ARGS = ("user_id", "room_id", "account_data_type")
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_account_data_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, room_id: str, account_data_type: str, content: JsonDict
    ) -> JsonDict:
        payload = {
            "content": content,
        }

        return payload

    async def _handle_request(  # type: ignore[override]
        self,
        request: Request,
        content: JsonDict,
        user_id: str,
        room_id: str,
        account_data_type: str,
    ) -> tuple[int, JsonDict]:
        max_stream_id = await self.handler.add_account_data_to_room(
            user_id, room_id, account_data_type, content["content"]
        )

        return 200, {"max_stream_id": max_stream_id}


class ReplicationRemoveRoomAccountDataRestServlet(ReplicationEndpoint):
    """Remove room account data on the appropriate account data worker.

    Request format:

        POST /_synapse/replication/remove_room_account_data/:user_id/:room_id/:account_data_type

        {
            "content": { ... },
        }

    """

    NAME = "remove_room_account_data"
    PATH_ARGS = ("user_id", "room_id", "account_data_type")
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_account_data_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, room_id: str, account_data_type: str, content: JsonDict
    ) -> JsonDict:
        return {}

    async def _handle_request(  # type: ignore[override]
        self,
        request: Request,
        content: JsonDict,
        user_id: str,
        room_id: str,
        account_data_type: str,
    ) -> tuple[int, JsonDict]:
        max_stream_id = await self.handler.remove_account_data_for_room(
            user_id, room_id, account_data_type
        )

        return 200, {"max_stream_id": max_stream_id}


class ReplicationAddTagRestServlet(ReplicationEndpoint):
    """Add tag on the appropriate account data worker.

    Request format:

        POST /_synapse/replication/add_tag/:user_id/:room_id/:tag

        {
            "content": { ... },
        }

    """

    NAME = "add_tag"
    PATH_ARGS = ("user_id", "room_id", "tag")
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_account_data_handler()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, room_id: str, tag: str, content: JsonDict
    ) -> JsonDict:
        payload = {
            "content": content,
        }

        return payload

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str, room_id: str, tag: str
    ) -> tuple[int, JsonDict]:
        max_stream_id = await self.handler.add_tag_to_room(
            user_id, room_id, tag, content["content"]
        )

        return 200, {"max_stream_id": max_stream_id}


class ReplicationRemoveTagRestServlet(ReplicationEndpoint):
    """Remove tag on the appropriate account data worker.

    Request format:

        POST /_synapse/replication/remove_tag/:user_id/:room_id/:tag

        {}

    """

    NAME = "remove_tag"
    PATH_ARGS = (
        "user_id",
        "room_id",
        "tag",
    )
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_account_data_handler()

    @staticmethod
    async def _serialize_payload(user_id: str, room_id: str, tag: str) -> JsonDict:  # type: ignore[override]
        return {}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str, room_id: str, tag: str
    ) -> tuple[int, JsonDict]:
        max_stream_id = await self.handler.remove_tag_from_room(
            user_id,
            room_id,
            tag,
        )

        return 200, {"max_stream_id": max_stream_id}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationAddUserAccountDataRestServlet(hs).register(http_server)
    ReplicationAddRoomAccountDataRestServlet(hs).register(http_server)
    ReplicationAddTagRestServlet(hs).register(http_server)
    ReplicationRemoveTagRestServlet(hs).register(http_server)

    if hs.config.experimental.msc3391_enabled:
        ReplicationRemoveUserAccountDataRestServlet(hs).register(http_server)
        ReplicationRemoveRoomAccountDataRestServlet(hs).register(http_server)
