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

Most changes to the map come with an event in the room (a join, a leave) and
are delivered alongside it. Changes without such an anchor, erasure today, are
recorded on the member info changes stream (`MemberInfoWorkerStore`) so that
syncing clients are woken and told.
"""

from typing import TYPE_CHECKING, Collection, Iterable, Mapping

from synapse.api.constants import EventTypes, MemberInfo
from synapse.events import EventBase
from synapse.types import JsonDict, StreamKeyType, StreamToken
from synapse.types.state import StateFilter

if TYPE_CHECKING:
    from synapse.server import HomeServer


class MemberInfoHandler:
    def __init__(self, hs: "HomeServer"):
        self._store = hs.get_datastores().main
        self._state_storage_controller = hs.get_storage_controllers().state
        self._notifier = hs.get_notifier()
        self.enabled = hs.config.experimental.msc4428_enabled

    async def on_user_erased(self, user_id: str) -> None:
        """Record that the erased user's `member_info` entry changed in every
        room they have any membership state in, and wake the members of those
        rooms. Nothing in the room anchors this change, hence the stream."""
        if not self.enabled:
            return

        room_ids = await self._store.get_rooms_with_membership_for_local_user(user_id)
        if not room_ids:
            return

        token = await self._store.add_member_info_changes(
            dict.fromkeys(room_ids, (user_id,))
        )
        self._notifier.on_new_event(StreamKeyType.MEMBER_INFO, token, rooms=room_ids)

    async def get_changes_in_rooms(
        self,
        room_ids: Collection[str],
        from_token: StreamToken,
        to_token: StreamToken,
    ) -> Mapping[str, Collection[str]] | None:
        """The stable IDs whose entry changed without an event anchor in the
        given rooms between the two tokens.

        Returns:
            room ID -> stable IDs, for rooms with changes only; or None if
            `from_token` is older than the retained changes, in which case the
            caller must send the full map (`member_info_for_response(full=True)`).
        """
        if not self.enabled:
            return {}
        return await self._store.get_member_info_changes_in_rooms(
            room_ids, from_token.member_info_key, to_token.member_info_key
        )

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
        *,
        changed: Iterable[str] = (),
        full: bool = False,
    ) -> JsonDict | None:
        """The `member_info` map for one room of a sync response.

        The map covers the stable IDs the response mentions: the senders of the
        events in it, the targets of its membership events, the room's heroes,
        and the `changed` stable IDs from the change stream. On an initial sync
        without lazy-loaded members that is every member of the room; a
        lazy-loading client gets the entries for the members it loads later
        from `/members`.

        Args:
            changed: stable IDs from `get_changes_in_rooms` for this room.
            full: also cover every current member of the room. For responses
                whose deltas cannot be computed (a token older than the change
                stream's retention, or a sliding sync room the connection has
                not seen since its last position).

        Returns:
            The map, or `None` if MSC4428 is disabled or there is nothing to say
            (so the field can be omitted from the response).
        """
        if not self.enabled:
            return None

        stable_ids = self.stable_ids_from_events(events)
        stable_ids.update(heroes)
        stable_ids.update(changed)

        if full:
            member_state = await self._state_storage_controller.get_current_state_ids(
                room_id,
                StateFilter.from_types([(EventTypes.Member, None)]),
                # A partial-state room is missing remote members; do not block
                # the sync on the resync for this fallback path.
                await_full_state=False,
            )
            stable_ids.update(state_key for _, state_key in member_state)

        return await self.get_member_info(room_id, stable_ids) or None
