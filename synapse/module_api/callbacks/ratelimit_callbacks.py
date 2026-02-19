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
from typing import TYPE_CHECKING, Awaitable, Callable

import attr

from synapse.util.async_helpers import delay_cancellation
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(auto_attribs=True)
class RatelimitOverride:
    """Represents a ratelimit being overridden."""

    per_second: float
    """The number of actions that can be performed in a second. `0.0` means that ratelimiting is disabled."""
    burst_count: int
    """How many actions that can be performed before being limited."""


GET_RATELIMIT_OVERRIDE_FOR_USER_CALLBACK = Callable[
    [str, str], Awaitable[RatelimitOverride | None]
]


class RatelimitModuleApiCallbacks:
    def __init__(self, hs: "HomeServer") -> None:
        self.server_name = hs.hostname
        self.clock = hs.get_clock()
        self._get_ratelimit_override_for_user_callbacks: list[
            GET_RATELIMIT_OVERRIDE_FOR_USER_CALLBACK
        ] = []

    def register_callbacks(
        self,
        get_ratelimit_override_for_user: GET_RATELIMIT_OVERRIDE_FOR_USER_CALLBACK
        | None = None,
    ) -> None:
        """Register callbacks from module for each hook."""
        if get_ratelimit_override_for_user is not None:
            self._get_ratelimit_override_for_user_callbacks.append(
                get_ratelimit_override_for_user
            )

    async def get_ratelimit_override_for_user(
        self, user_id: str, limiter_name: str
    ) -> RatelimitOverride | None:
        for callback in self._get_ratelimit_override_for_user_callbacks:
            with Measure(
                self.clock,
                name=f"{callback.__module__}.{callback.__qualname__}",
                server_name=self.server_name,
            ):
                res: RatelimitOverride | None = await delay_cancellation(
                    callback(user_id, limiter_name)
                )
            if res:
                return res

        return None
