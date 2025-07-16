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
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, List, Optional

from synapse.types import JsonDict
from synapse.util.async_helpers import delay_cancellation
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

GET_MEDIA_CONFIG_FOR_USER_CALLBACK = Callable[[str], Awaitable[Optional[JsonDict]]]

IS_USER_ALLOWED_TO_UPLOAD_MEDIA_OF_SIZE_CALLBACK = Callable[[str, int], Awaitable[bool]]


class MediaRepositoryModuleApiCallbacks:
    def __init__(self, hs: "HomeServer") -> None:
        self.server_name = hs.hostname
        self.clock = hs.get_clock()
        self._get_media_config_for_user_callbacks: List[
            GET_MEDIA_CONFIG_FOR_USER_CALLBACK
        ] = []
        self._is_user_allowed_to_upload_media_of_size_callbacks: List[
            IS_USER_ALLOWED_TO_UPLOAD_MEDIA_OF_SIZE_CALLBACK
        ] = []

    def register_callbacks(
        self,
        get_media_config_for_user: Optional[GET_MEDIA_CONFIG_FOR_USER_CALLBACK] = None,
        is_user_allowed_to_upload_media_of_size: Optional[
            IS_USER_ALLOWED_TO_UPLOAD_MEDIA_OF_SIZE_CALLBACK
        ] = None,
    ) -> None:
        """Register callbacks from module for each hook."""
        if get_media_config_for_user is not None:
            self._get_media_config_for_user_callbacks.append(get_media_config_for_user)

        if is_user_allowed_to_upload_media_of_size is not None:
            self._is_user_allowed_to_upload_media_of_size_callbacks.append(
                is_user_allowed_to_upload_media_of_size
            )

    async def get_media_config_for_user(self, user_id: str) -> Optional[JsonDict]:
        for callback in self._get_media_config_for_user_callbacks:
            with Measure(
                self.clock,
                name=f"{callback.__module__}.{callback.__qualname__}",
                server_name=self.server_name,
            ):
                res: Optional[JsonDict] = await delay_cancellation(callback(user_id))
            if res:
                return res

        return None

    async def is_user_allowed_to_upload_media_of_size(
        self, user_id: str, size: int
    ) -> bool:
        for callback in self._is_user_allowed_to_upload_media_of_size_callbacks:
            with Measure(
                self.clock,
                name=f"{callback.__module__}.{callback.__qualname__}",
                server_name=self.server_name,
            ):
                res: bool = await delay_cancellation(callback(user_id, size))
            if not res:
                return res

        return True
