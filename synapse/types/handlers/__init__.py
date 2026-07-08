#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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


from typing import TypedDict

from synapse.api.constants import EventTypes

# Sliding Sync: The event types that clients should consider as new activity and affect
# the `bump_stamp`
SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES = {
    EventTypes.Create,
    EventTypes.Message,
    EventTypes.Encrypted,
    EventTypes.Sticker,
    EventTypes.CallInvite,
    EventTypes.PollStart,
    EventTypes.LiveLocationShareStart,
}


class ShutdownRoomParams(TypedDict):
    """
    Attributes:
        requester_user_id:
            User who requested the action. Will be recorded as putting the room on the
            blocking list.
        new_room_user_id:
            If set, a new room will be created with this user ID
            as the creator and admin, and all users in the old room will be
            moved into that room. If not set, no new room will be created
            and the users will just be removed from the old room.
        new_room_name:
            A string representing the name of the room that new users will
            be invited to. Defaults to `Content Violation Notification`
        message:
            A string containing the first message that will be sent as
            `new_room_user_id` in the new room. Ideally this will clearly
            convey why the original room was shut down.
            Defaults to `Sharing illegal content on this server is not
            permitted and rooms in violation will be blocked.`
        block:
            If set to `true`, this room will be added to a blocking list,
            preventing future attempts to join the room. Defaults to `false`.
        purge:
            If set to `true`, purge the given room from the database.
        force_purge:
            If set to `true`, the room will be purged from database
            even if there are still users joined to the room.
    """

    requester_user_id: str | None
    new_room_user_id: str | None
    new_room_name: str | None
    message: str | None
    block: bool
    purge: bool
    force_purge: bool


class ShutdownRoomResponse(TypedDict):
    """
    Attributes:
        kicked_users: An array of users (`user_id`) that were kicked.
        failed_to_kick_users:
            An array of users (`user_id`) that that were not kicked.
        local_aliases:
            An array of strings representing the local aliases that were
            migrated from the old room to the new.
        new_room_id: A string representing the room ID of the new room.
    """

    kicked_users: list[str]
    failed_to_kick_users: list[str]
    local_aliases: list[str]
    new_room_id: str | None
