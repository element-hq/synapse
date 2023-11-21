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
from typing import TYPE_CHECKING, Optional

from synapse.api.errors import Codes, StoreError, SynapseError
from synapse.handlers.device import DeviceHandler
from synapse.types import Requester

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class SetPasswordHandler:
    """Handler which deals with changing user account passwords"""

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._auth_handler = hs.get_auth_handler()
        # This can only be instantiated on the main process.
        device_handler = hs.get_device_handler()
        assert isinstance(device_handler, DeviceHandler)
        self._device_handler = device_handler

    async def set_password(
        self,
        user_id: str,
        password_hash: str,
        logout_devices: bool,
        requester: Optional[Requester] = None,
    ) -> None:
        if not self._auth_handler.can_change_password():
            raise SynapseError(403, "Password change disabled", errcode=Codes.FORBIDDEN)

        try:
            await self.store.user_set_password_hash(user_id, password_hash)
        except StoreError as e:
            if e.code == 404:
                raise SynapseError(404, "Unknown user", Codes.NOT_FOUND)
            raise e

        # Optionally, log out all of the user's other sessions.
        if logout_devices:
            except_device_id = requester.device_id if requester else None
            except_access_token_id = requester.access_token_id if requester else None

            # First delete all of their other devices.
            await self._device_handler.delete_all_devices_for_user(
                user_id, except_device_id=except_device_id
            )

            # and now delete any access tokens which weren't associated with
            # devices (or were associated with this device).
            await self._auth_handler.delete_access_tokens_for_user(
                user_id, except_token_id=except_access_token_id
            )
