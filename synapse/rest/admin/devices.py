#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Dirk Klimpel
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
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import NotFoundError, SynapseError
from synapse.http.servlet import (
    RestServlet,
    assert_params_in_dict,
    parse_json_object_from_request,
)
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DeviceRestServlet(RestServlet):
    """
    Get, update or delete the given user's device
    """

    PATTERNS = admin_patterns(
        "/users/(?P<user_id>[^/]*)/devices/(?P<device_id>[^/]*)$", "v2"
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.device_handler = hs.get_device_handler()
        self.store = hs.get_datastores().main
        self.is_mine = hs.is_mine

    async def on_GET(
        self, request: SynapseRequest, user_id: str, device_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only lookup local users")

        u = await self.store.get_user_by_id(target_user.to_string())
        if u is None:
            raise NotFoundError("Unknown user")

        device = await self.device_handler.get_device(
            target_user.to_string(), device_id
        )
        if device is None:
            raise NotFoundError("No device found")
        return HTTPStatus.OK, device

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str, device_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only lookup local users")

        u = await self.store.get_user_by_id(target_user.to_string())
        if u is None:
            raise NotFoundError("Unknown user")

        await self.device_handler.delete_devices(target_user.to_string(), [device_id])
        return HTTPStatus.OK, {}

    async def on_PUT(
        self, request: SynapseRequest, user_id: str, device_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only lookup local users")

        u = await self.store.get_user_by_id(target_user.to_string())
        if u is None:
            raise NotFoundError("Unknown user")

        body = parse_json_object_from_request(request, allow_empty_body=True)
        await self.device_handler.update_device(
            target_user.to_string(), device_id, body
        )
        return HTTPStatus.OK, {}


class DevicesRestServlet(RestServlet):
    """
    Retrieve the given user's devices

    This can be mounted on workers as it is read-only, as opposed
    to `DevicesRestServlet`.
    """

    PATTERNS = admin_patterns("/users/(?P<user_id>[^/]*)/devices$", "v2")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.device_worker_handler = hs.get_device_handler()
        self.store = hs.get_datastores().main
        self.is_mine = hs.is_mine

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only lookup local users")

        u = await self.store.get_user_by_id(target_user.to_string())
        if u is None:
            raise NotFoundError("Unknown user")

        devices = await self.device_worker_handler.get_devices_by_user(
            target_user.to_string()
        )

        # mark the dehydrated device by adding a "dehydrated" flag
        dehydrated_device_info = await self.device_worker_handler.get_dehydrated_device(
            target_user.to_string()
        )
        if dehydrated_device_info:
            dehydrated_device_id = dehydrated_device_info[0]
            for device in devices:
                is_dehydrated = device["device_id"] == dehydrated_device_id
                device["dehydrated"] = is_dehydrated

        return HTTPStatus.OK, {"devices": devices, "total": len(devices)}

    async def on_POST(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, JsonDict]:
        """Creates a new device for the user."""
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Can only create devices for local users"
            )

        u = await self.store.get_user_by_id(target_user.to_string())
        if u is None:
            raise NotFoundError("Unknown user")

        body = parse_json_object_from_request(request)
        device_id = body.get("device_id")
        if not device_id:
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Missing device_id")
        if not isinstance(device_id, str):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "device_id must be a string")

        await self.device_worker_handler.check_device_registered(
            user_id=user_id, device_id=device_id
        )

        return HTTPStatus.CREATED, {}


class DeleteDevicesRestServlet(RestServlet):
    """
    API for bulk deletion of devices. Accepts a JSON object with a devices
    key which lists the device_ids to delete.
    """

    PATTERNS = admin_patterns("/users/(?P<user_id>[^/]*)/delete_devices$", "v2")

    def __init__(self, hs: "HomeServer"):
        self.auth = hs.get_auth()
        self.device_handler = hs.get_device_handler()
        self.store = hs.get_datastores().main
        self.is_mine = hs.is_mine

    async def on_POST(
        self, request: SynapseRequest, user_id: str
    ) -> tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        target_user = UserID.from_string(user_id)
        if not self.is_mine(target_user):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only lookup local users")

        u = await self.store.get_user_by_id(target_user.to_string())
        if u is None:
            raise NotFoundError("Unknown user")

        body = parse_json_object_from_request(request, allow_empty_body=False)
        assert_params_in_dict(body, ["devices"])

        await self.device_handler.delete_devices(
            target_user.to_string(), body["devices"]
        )
        return HTTPStatus.OK, {}
