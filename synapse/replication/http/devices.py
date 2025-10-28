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

import logging
from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.logging.opentracing import active_span
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict, JsonMapping

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationNotifyDeviceUpdateRestServlet(ReplicationEndpoint):
    """Notify a device writer that a user's device list has changed.

    Request format:

        POST /_synapse/replication/notify_device_update/:user_id

        {
            "device_ids": ["JLAFKJWSCS", "JLAFKJWSCS"]
        }
    """

    NAME = "notify_device_update"
    PATH_ARGS = ("user_id",)
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.device_handler = hs.get_device_handler()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()

    @staticmethod
    async def _serialize_payload(  # type: ignore[override]
        user_id: str, device_ids: list[str]
    ) -> JsonDict:
        return {"device_ids": device_ids}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, user_id: str
    ) -> tuple[int, JsonDict]:
        device_ids = content["device_ids"]

        span = active_span()
        if span:
            span.set_tag("user_id", user_id)
            span.set_tag("device_ids", f"{device_ids!r}")

        await self.device_handler.notify_device_update(user_id, device_ids)

        return 200, {}


class ReplicationNotifyUserSignatureUpdateRestServlet(ReplicationEndpoint):
    """Notify a device writer that a user have made new signatures of other users.

    Request format:

        POST /_synapse/replication/notify_user_signature_update/:from_user_id

        {
            "user_ids": ["@alice:example.org", "@bob:example.org", ...]
        }
    """

    NAME = "notify_user_signature_update"
    PATH_ARGS = ("from_user_id",)
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.device_handler = hs.get_device_handler()
        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()

    @staticmethod
    async def _serialize_payload(from_user_id: str, user_ids: list[str]) -> JsonDict:  # type: ignore[override]
        return {"user_ids": user_ids}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, from_user_id: str
    ) -> tuple[int, JsonDict]:
        user_ids = content["user_ids"]

        span = active_span()
        if span:
            span.set_tag("from_user_id", from_user_id)
            span.set_tag("user_ids", f"{user_ids!r}")

        await self.device_handler.notify_user_signature_update(from_user_id, user_ids)

        return 200, {}


class ReplicationMultiUserDevicesResyncRestServlet(ReplicationEndpoint):
    """Ask master to resync the device list for multiple users from the same
    remote server by contacting their server.

    This must happen on master so that the results can be correctly cached in
    the database and streamed to workers.

    Request format:

        POST /_synapse/replication/multi_user_device_resync

        {
            "user_ids": ["@alice:example.org", "@bob:example.org", ...]
        }

    Response is roughly equivalent to ` /_matrix/federation/v1/user/devices/:user_id`
    response, but there is a map from user ID to response, e.g.:

        {
            "@alice:example.org": {
                "devices": [
                    {
                        "device_id": "JLAFKJWSCS",
                        "keys": { ... },
                        "device_display_name": "Alice's Mobile Phone"
                    }
                ]
            },
            ...
        }
    """

    NAME = "multi_user_device_resync"
    PATH_ARGS = ()
    CACHE = True

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.device_list_updater = hs.get_device_handler().device_list_updater

        self.store = hs.get_datastores().main
        self.clock = hs.get_clock()

    @staticmethod
    async def _serialize_payload(user_ids: list[str]) -> JsonDict:  # type: ignore[override]
        return {"user_ids": user_ids}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict
    ) -> tuple[int, dict[str, JsonMapping | None]]:
        user_ids: list[str] = content["user_ids"]

        logger.info("Resync for %r", user_ids)
        span = active_span()
        if span:
            span.set_tag("user_ids", f"{user_ids!r}")

        multi_user_devices = await self.device_list_updater.multi_user_device_resync(
            user_ids
        )

        return 200, multi_user_devices


class ReplicationHandleNewDeviceUpdateRestServlet(ReplicationEndpoint):
    """Wake up a device writer to send local device list changes as federation outbound pokes.

    Request format:

        POST /_synapse/replication/handle_new_device_update

        {}
    """

    NAME = "handle_new_device_update"
    PATH_ARGS = ()
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.device_handler = hs.get_device_handler()

    @staticmethod
    async def _serialize_payload() -> JsonDict:  # type: ignore[override]
        return {}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict
    ) -> tuple[int, JsonDict]:
        await self.device_handler.handle_new_device_update()
        return 200, {}


class ReplicationDeviceHandleRoomUnPartialStated(ReplicationEndpoint):
    """Handles sending appropriate device list updates in a room that has
    gone from partial to full state.

    Request format:

        POST /_synapse/replication/device_handle_room_un_partial_stated/:room_id

        {}
    """

    NAME = "device_handle_room_un_partial_stated"
    PATH_ARGS = ("room_id",)
    CACHE = True

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.device_handler = hs.get_device_handler()

    @staticmethod
    async def _serialize_payload(room_id: str) -> JsonDict:  # type: ignore[override]
        return {}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict, room_id: str
    ) -> tuple[int, JsonDict]:
        await self.device_handler.handle_room_un_partial_stated(room_id)
        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationNotifyDeviceUpdateRestServlet(hs).register(http_server)
    ReplicationNotifyUserSignatureUpdateRestServlet(hs).register(http_server)
    ReplicationMultiUserDevicesResyncRestServlet(hs).register(http_server)
    ReplicationHandleNewDeviceUpdateRestServlet(hs).register(http_server)
    ReplicationDeviceHandleRoomUnPartialStated(hs).register(http_server)
