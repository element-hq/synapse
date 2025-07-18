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
from typing import TYPE_CHECKING, Optional, Tuple

from synapse._pydantic_compat import BaseModel, StrictStr
from synapse.api.errors import NotFoundError
from synapse.http.servlet import parse_and_validate_json_object_from_request
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


from ._base import MasBaseResource

logger = logging.getLogger(__name__)


class MasCreateDeviceResource(MasBaseResource):
    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(BaseModel):
        localpart: StrictStr
        device_id: StrictStr
        display_name: Optional[StrictStr]

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> Tuple[int, JsonDict]:
        self.assert_mas_request(request)

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
    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(BaseModel):
        localpart: StrictStr
        device_id: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> Tuple[int, JsonDict]:
        self.assert_mas_request(request)

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
    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(BaseModel):
        localpart: StrictStr
        device_id: StrictStr
        display_name: StrictStr

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> Tuple[int, JsonDict]:
        self.assert_mas_request(request)

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
    def __init__(self, hs: "HomeServer"):
        MasBaseResource.__init__(self, hs)

        self.device_handler = hs.get_device_handler()

    class PostBody(BaseModel):
        localpart: StrictStr
        devices: set[StrictStr]

    async def _async_render_POST(
        self, request: "SynapseRequest"
    ) -> Tuple[int, JsonDict]:
        self.assert_mas_request(request)

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

        # Log what we're about to do, as this can be an expensive operation
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
