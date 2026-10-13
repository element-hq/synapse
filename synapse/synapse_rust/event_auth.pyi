# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import TYPE_CHECKING, Iterable

from synapse.api.room_versions import RoomVersion
from synapse.synapse_rust.events import Event
from synapse.types import StateKey

if TYPE_CHECKING:
    from synapse.events.builder import EventBuilder

def check_state_dependent_auth_rules(
    event: Event, auth_events: Iterable[Event]
) -> None:
    """Check that an event complies with the auth rules that depend on room
    state.

    Raises:
        AuthError (or UnstableSpecAuthError) if the event is not allowed.
        SynapseError if the event or its auth events are malformed.
    """

def auth_types_for_event(
    room_version: RoomVersion, event: Event | "EventBuilder"
) -> set[StateKey]:
    """The (type, state_key) pairs that may be needed to auth the event."""
