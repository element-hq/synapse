#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
#

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from pydantic import StrictStr

from synapse.api.errors import NotFoundError
from synapse.http.servlet import parse_and_validate_json_object_from_request
from synapse.types import JsonDict, UserID
from synapse.types.rest import RequestBodyModel

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


from ._base import MasBaseResource

logger = logging.getLogger(__name__)


class MasUpsertDeviceResource(MasBaseResource):
    """
    Endpoint for MAS to create or update user devices.

    Takes a localpart, device ID, and optional display name to create new devices
    or update existing ones.

    POST /_synapse/mas/upsert_device
    {"localpart": "alice", "device_id": "DEVICE123", "display_name": "Alice's Phone"}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr
        device_id: StrictStr
        display_name: StrictStr | None = None

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        inserted = await self.device_handler.upsert_device(
            user_id=str(user_id),
            device_id=body.device_id,
            display_name=body.display_name,
        )

        return HTTPStatus.CREATED if inserted else HTTPStatus.OK, {}


class MasDeleteDeviceResource(MasBaseResource):
    """
    Endpoint for MAS to delete user devices.

    Takes a localpart and device ID to remove the specified device from the user's account.

    POST /_synapse/mas/delete_device
    {"localpart": "alice", "device_id": "DEVICE123"}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr
        device_id: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        await self.device_handler.delete_devices(
            user_id=str(user_id),
            device_ids=[body.device_id],
        )

        return HTTPStatus.NO_CONTENT, {}


class MasUpdateDeviceDisplayNameResource(MasBaseResource):
    """
    Endpoint for MAS to update a device's display name.

    Takes a localpart, device ID, and new display name to update the device's name.

    POST /_synapse/mas/update_device_display_name
    {"localpart": "alice", "device_id": "DEVICE123", "display_name": "Alice's New Phone"}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr
        device_id: StrictStr
        display_name: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        await self.device_handler.update_device(
            user_id=str(user_id),
            device_id=body.device_id,
            content={"display_name": body.display_name},
        )

        return HTTPStatus.OK, {}


class MasSyncDevicesResource(MasBaseResource):
    """
    Endpoint for MAS to synchronize a user's complete device list.

    Takes a localpart and a set of device IDs to ensure the user's device list
    matches the provided set by adding missing devices and removing extra ones.

    POST /_synapse/mas/sync_devices
    {"localpart": "alice", "devices": ["DEVICE123", "DEVICE456"]}
    """

    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(RequestBodyModel):
        localpart: StrictStr
        devices: list[str]

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> tuple[int, JsonDict]:
        self.assert_request_is_from_mas(request)

        body = parse_and_validate_json_object_from_request(request, self.PostBody)
        user_id = UserID(body.localpart, self.hostname)

        # Check the user exists
        user = await self.store.get_user_by_id(user_id=str(user_id))
        if user is None:
            raise NotFoundError("User not found")

        current_devices = await self.store.get_devices_by_user(user_id=str(user_id))
        current_devices_list = set(current_devices.keys())
        target_device_list = set(body.devices)

        to_add = target_device_list - current_devices_list
        to_delete = current_devices_list - target_device_list

        # Log what we're about to do to make it easier to debug if it stops
        # mid-way, as this can be a long operation if there are a lot of devices
        # to delete or to add.
        if to_add and to_delete:
            logger.info(
                "Syncing %d devices for user %s will add %d devices and delete %d devices",
                len(target_device_list),
                user_id,
                len(to_add),
                len(to_delete),
            )
        elif to_add:
            logger.info(
                "Syncing %d devices for user %s will add %d devices",
                len(target_device_list),
                user_id,
                len(to_add),
            )
        elif to_delete:
            logger.info(
                "Syncing %d devices for user %s will delete %d devices",
                len(target_device_list),
                user_id,
                len(to_delete),
            )

        if to_delete:
            await self.device_handler.delete_devices(
                user_id=str(user_id), device_ids=to_delete
            )

        for device_id in to_add:
            await self.device_handler.upsert_device(
                user_id=str(user_id),
                device_id=device_id,
            )

        return 200, {}
