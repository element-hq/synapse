#
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
#

from typing import TypedDict

from typing_extensions import NotRequired, Unpack

from synapse.api.room_versions import (
    RoomVersion,
    RoomVersions,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.federation.federation_base import event_from_pdu_json
from synapse.types import JsonDict


def default_event_fields(room_version: RoomVersion) -> JsonDict:
    """Return default values for every field required by `room_version`."""

    # We need to include entries for every required field for the room version.
    # Note that they don't necessarily have to be valid values, just enough to
    # allow us to construct the event class. (Ideally we'd build a fully valid
    # event, but this is fine for now.)
    defaults: JsonDict = {
        "type": "m.test",
        "sender": "@test:test",
        "content": {},
        "depth": 1,
        "origin_server_ts": 1,
        "hashes": {},
        "prev_events": [],
        "room_id": "!test:test",
    }

    # MSC4242 versions require prev_state_events but not auth_events.
    if room_version.msc4242_state_dags:
        defaults["prev_state_events"] = []
    else:
        defaults["auth_events"] = []

    if room_version == RoomVersions.V1:
        # V1 requires an event_id field, but later versions don't.
        defaults["event_id"] = "$test_event_id:matrix.org"

    return defaults


def make_test_event(
    event_dict: JsonDict | None = None,
    room_version: RoomVersion = RoomVersions.V1,
    internal_metadata_dict: JsonDict | None = None,
    rejected_reason: str | None = None,
    **fields: Unpack["_EventFields"],
) -> EventBase:
    """Build an `EventBase` with defaults for the strict-required fields.

    Pass an `event_dict` and/or `**fields` keyword arguments — both are
    merged on top of the format-version defaults from
    `default_event_fields`. Explicit values win over defaults, and
    `**fields` wins over `event_dict` so call sites can override a
    shared base dict with one-off tweaks.
    """
    merged: JsonDict = {
        **default_event_fields(room_version),
        **(event_dict or {}),
        **fields,
    }
    return make_event_from_dict(
        merged,
        room_version=room_version,
        internal_metadata_dict=internal_metadata_dict,
        rejected_reason=rejected_reason,
    )


def make_test_pdu_event(
    pdu: JsonDict,
    room_version: RoomVersion,
    received_time: int | None = None,
) -> EventBase:
    """Wrapper around `event_from_pdu_json` for test PDU dicts.

    Federation-side test fixtures often omit fields the strict Rust ctor
    requires (e.g. `hashes`, `auth_events`, `prev_events`, `depth`)
    because those tests focus on transport/auth flow rather than event
    well-formedness. This helper layers in the same format-version
    defaults as `make_test_event` before delegating.
    """
    pdu = {**default_event_fields(room_version), **pdu}
    return event_from_pdu_json(pdu, room_version, received_time=received_time)


class _EventFields(TypedDict):
    """Type for `kwargs` in `make_test_event`."""

    event_id: NotRequired[str]
    type: NotRequired[str]
    sender: NotRequired[str]
    content: NotRequired[JsonDict]
    depth: NotRequired[int]
    origin_server_ts: NotRequired[int]
    hashes: NotRequired[dict]
    auth_events: NotRequired[list]
    prev_events: NotRequired[list]
    room_id: NotRequired[str]
