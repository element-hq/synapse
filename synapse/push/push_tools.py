#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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
from typing import Dict

from synapse.api.constants import EventTypes, Membership
from synapse.events import EventBase
from synapse.push.presentable_names import calculate_room_name, name_from_member_event
from synapse.storage.controllers import StorageControllers
from synapse.storage.databases.main import DataStore


async def get_badge_count(store: DataStore, user_id: str, group_by_room: bool) -> int:
    invites = await store.get_invited_rooms_for_local_user(user_id)
    joins = await store.get_rooms_for_user(user_id)

    badge = len(invites)

    room_to_count = await store.get_unread_counts_by_room_for_user(user_id)
    for room_id, notify_count in room_to_count.items():
        # room_to_count may include rooms which the user has left,
        # ignore those.
        if room_id not in joins:
            continue

        if notify_count == 0:
            continue

        if group_by_room:
            # return one badge count per conversation
            badge += 1
        else:
            # Increase badge by number of notifications in room
            # NOTE: this includes threaded and unthreaded notifications.
            badge += notify_count

    return badge


async def get_context_for_event(
    storage: StorageControllers, ev: EventBase, user_id: str
) -> Dict[str, str]:
    ctx: Dict[str, str] = {}

    if ev.internal_metadata.outlier:
        # We don't have state for outliers, so we can't compute the context
        # except for invites and knocks. (Such events are known as 'out-of-band
        # memberships' for the user).
        if ev.type != EventTypes.Member:
            return ctx

        # We might be able to pull out the display name for the sender straight
        # from the membership event
        event_display_name = ev.content.get("displayname")
        if event_display_name and ev.state_key == ev.sender:
            ctx["sender_display_name"] = event_display_name

        room_state = []
        if ev.content.get("membership") == Membership.INVITE:
            room_state = ev.unsigned.get("invite_room_state", [])
        elif ev.content.get("membership") == Membership.KNOCK:
            room_state = ev.unsigned.get("knock_room_state", [])

        # Ideally we'd reuse the logic in `calculate_room_name`, but that gets
        # complicated to handle partial events vs pulling events from the DB.
        for state_dict in room_state:
            type_tuple = (state_dict["type"], state_dict.get("state_key"))
            if type_tuple == (EventTypes.Member, ev.sender):
                display_name = state_dict["content"].get("displayname")
                if display_name:
                    ctx["sender_display_name"] = display_name
            elif type_tuple == (EventTypes.Name, ""):
                room_name = state_dict["content"].get("name")
                if room_name:
                    ctx["name"] = room_name

        return ctx

    room_state_ids = await storage.state.get_state_ids_for_event(ev.event_id)

    # we no longer bother setting room_alias, and make room_name the
    # human-readable name instead, be that m.room.name, an alias or
    # a list of people in the room
    name = await calculate_room_name(
        storage.main, room_state_ids, user_id, fallback_to_single_member=False
    )
    if name:
        ctx["name"] = name

    sender_state_event_id = room_state_ids[("m.room.member", ev.sender)]
    sender_state_event = await storage.main.get_event(sender_state_event_id)
    ctx["sender_display_name"] = name_from_member_event(sender_state_event)

    return ctx
