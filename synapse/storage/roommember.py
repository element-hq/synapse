#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

import attr

from synapse.types import PersistedEventPosition

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class RoomsForUser:
    room_id: str
    sender: str
    membership: str
    event_id: str
    event_pos: PersistedEventPosition
    room_version_id: str


@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class RoomsForUserSlidingSync:
    room_id: str
    sender: str | None
    membership: str
    event_id: str | None
    event_pos: PersistedEventPosition
    room_version_id: str

    has_known_state: bool
    room_type: str | None
    is_encrypted: bool


@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class RoomsForUserStateReset:
    """A version of `RoomsForUser` that supports optional sender and event ID
    fields, to handle state resets. State resets can affect room membership
    without a corresponding event so that information isn't always available."""

    room_id: str
    sender: str | None
    membership: str
    event_id: str | None
    event_pos: PersistedEventPosition
    room_version_id: str


@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class GetRoomsForUserWithStreamOrdering:
    room_id: str
    event_pos: PersistedEventPosition


@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class ProfileInfo:
    avatar_url: str | None
    display_name: str | None


# TODO This is used as a cached value and is mutable.
@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class MemberSummary:
    # A truncated list of (user_id, event_id) tuples for users of a given
    # membership type, suitable for use in calculating heroes for a room.
    members: list[tuple[str, str]]
    # The total number of users of a given membership type.
    count: int
