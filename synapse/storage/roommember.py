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
from typing import List, Optional, Tuple

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
class GetRoomsForUserWithStreamOrdering:
    room_id: str
    event_pos: PersistedEventPosition


@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class ProfileInfo:
    avatar_url: Optional[str]
    display_name: Optional[str]


# TODO This is used as a cached value and is mutable.
@attr.s(slots=True, frozen=True, weakref_slot=False, auto_attribs=True)
class MemberSummary:
    # A truncated list of (user_id, event_id) tuples for users of a given
    # membership type, suitable for use in calculating heroes for a room.
    members: List[Tuple[str, str]]
    # The total number of users of a given membership type.
    count: int
