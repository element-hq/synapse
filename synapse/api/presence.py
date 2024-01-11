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

from typing import Any, Optional

import attr

from synapse.api.constants import PresenceState
from synapse.types import JsonDict


@attr.s(slots=True, auto_attribs=True)
class UserDevicePresenceState:
    """
    Represents the current presence state of a user's device.

    user_id: The user ID.
    device_id: The user's device ID.
    state: The presence state, see PresenceState.
    last_active_ts: Time in msec that the device last interacted with server.
    last_sync_ts: Time in msec that the device last *completed* a sync
        (or event stream).
    """

    user_id: str
    device_id: Optional[str]
    state: str
    last_active_ts: int
    last_sync_ts: int

    @classmethod
    def default(
        cls, user_id: str, device_id: Optional[str]
    ) -> "UserDevicePresenceState":
        """Returns a default presence state."""
        return cls(
            user_id=user_id,
            device_id=device_id,
            state=PresenceState.OFFLINE,
            last_active_ts=0,
            last_sync_ts=0,
        )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class UserPresenceState:
    """Represents the current presence state of the user.

    user_id: The user ID.
    state: The presence state, see PresenceState.
    last_active_ts: Time in msec that the user last interacted with server.
    last_federation_update_ts: Time in msec since either a) we sent a presence
        update to other servers or b) we received a presence update, depending
        on if is a local user or not.
    last_user_sync_ts: Time in msec that the user last *completed* a sync
        (or event stream).
    status_msg: User set status message.
    currently_active: True if the user is currently syncing.
    """

    user_id: str
    state: str
    last_active_ts: int
    last_federation_update_ts: int
    last_user_sync_ts: int
    status_msg: Optional[str]
    currently_active: bool

    def as_dict(self) -> JsonDict:
        return attr.asdict(self)

    def copy_and_replace(self, **kwargs: Any) -> "UserPresenceState":
        return attr.evolve(self, **kwargs)

    @classmethod
    def default(cls, user_id: str) -> "UserPresenceState":
        """Returns a default presence state."""
        return cls(
            user_id=user_id,
            state=PresenceState.OFFLINE,
            last_active_ts=0,
            last_federation_update_ts=0,
            last_user_sync_ts=0,
            status_msg=None,
            currently_active=False,
        )
