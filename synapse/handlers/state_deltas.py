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
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MatchChange(Enum):
    no_change = auto()
    now_true = auto()
    now_false = auto()


class StateDeltasHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main

    async def _get_key_change(
        self,
        prev_event_id: str | None,
        event_id: str | None,
        key_name: str,
        public_value: str,
    ) -> MatchChange:
        """Given two events check if the `key_name` field in content changed
        from not matching `public_value` to doing so.

        For example, check if `history_visibility` (`key_name`) changed from
        `shared` to `world_readable` (`public_value`).
        """
        prev_event = None
        event = None
        if prev_event_id:
            prev_event = await self.store.get_event(prev_event_id, allow_none=True)

        if event_id:
            event = await self.store.get_event(event_id, allow_none=True)

        if not event and not prev_event:
            logger.debug("Neither event exists: %r %r", prev_event_id, event_id)
            return MatchChange.no_change

        prev_value = None
        value = None

        if prev_event:
            prev_value = prev_event.content.get(key_name)

        if event:
            value = event.content.get(key_name)

        logger.debug("prev_value: %r -> value: %r", prev_value, value)

        if value == public_value and prev_value != public_value:
            return MatchChange.now_true
        elif value != public_value and prev_value == public_value:
            return MatchChange.now_false
        else:
            return MatchChange.no_change
