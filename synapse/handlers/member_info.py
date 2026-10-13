#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""MSC4428: the per-room `member_info` map.

`member_info` maps a room member's stable ID (today: their user ID, carried on
events as `unsigned.org.matrix.msc4428.stable_id`) to the user ID it currently
stands for, which may be `None` once the user is erased. Entries whose user ID
equals the stable ID are omitted, as the MSC allows, so in today's rooms the map
only ever names erased users.
"""

from typing import TYPE_CHECKING, Collection, Iterable

from synapse.api.constants import EventTypes, MemberInfo
from synapse.events import EventBase
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class MemberInfoHandler:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self.enabled = hs.config.experimental.msc4428_enabled

    @staticmethod
    def stable_ids_from_events(events: Iterable[EventBase]) -> set[str]:
        """The stable IDs a client learns about from a set of events: the
        senders, and the targets of membership events."""
        stable_ids: set[str] = set()
        for event in events:
            stable_ids.add(event.sender)
            if event.type == EventTypes.Member and event.is_state():
                stable_ids.add(event.state_key)
        return stable_ids

    async def get_member_info(
        self, room_id: str, stable_ids: Collection[str]
    ) -> JsonDict:
        """The `member_info` entries for the given stable IDs.

        Only stable IDs whose mapping differs from the stable ID itself get an
        entry; today that is erased users, whose user ID is `None`.

        Args:
            room_id: The room the stable IDs belong to.
            stable_ids: The stable IDs to look up.

        Returns:
            The map, possibly empty.
        """
        if not stable_ids:
            return {}

        erased = await self._store.are_users_erased(stable_ids)
        return {
            stable_id: {MemberInfo.USER_ID: None}
            for stable_id, is_erased in erased.items()
            if is_erased
        }

    async def member_info_for_response(
        self,
        room_id: str,
        events: Iterable[EventBase],
        heroes: Iterable[str] = (),
    ) -> JsonDict | None:
        """The `member_info` map for one room of a sync response.

        The map covers the stable IDs the response mentions: the senders of the
        events in it, the targets of its membership events, and the room's
        heroes. On an initial sync without lazy-loaded members that is every
        member of the room; a lazy-loading client gets the entries for the
        members it loads later from `/members`.

        Returns:
            The map, or `None` if MSC4428 is disabled or there is nothing to say
            (so the field can be omitted from the response).
        """
        if not self.enabled:
            return None

        stable_ids = self.stable_ids_from_events(events)
        stable_ids.update(heroes)

        return await self.get_member_info(room_id, stable_ids) or None
