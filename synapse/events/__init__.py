#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

import collections.abc
from typing import (
    TYPE_CHECKING,
    Any,
    TypeAlias,
    Union,
)

import attr

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    RelationTypes,
)
from synapse.api.errors import Codes, SynapseError
from synapse.api.room_versions import RoomVersion, RoomVersions
from synapse.synapse_rust.events import Event
from synapse.types import (
    JsonDict,
    StateKey,
)

if TYPE_CHECKING:
    from synapse.events.builder import EventBuilder

# The base class for events used to be called EventBase, but it was renamed to
# Event when we switched to using the Rust implementation. We keep the old name
# around for backwards compatibility.
EventBase: TypeAlias = Event


USE_FROZEN_DICTS = False
"""
Whether we should use frozen_dict in FrozenEvent. Using frozen_dicts prevents
bugs where we accidentally share e.g. signature dicts. However, converting a
dict to frozen_dicts is expensive.

FIXME: Remove `USE_FROZEN_DICTS` and `use_frozen_dicts` config as this is no
longer used since we switched to using the Rust implementation, all events are
immutable already (and so don't benefit from freezing).
"""


def make_event_from_dict(
    event_dict: JsonDict,
    room_version: RoomVersion = RoomVersions.V1,
    internal_metadata_dict: JsonDict | None = None,
    rejected_reason: str | None = None,
) -> Event:
    """Construct an EventBase from the given event dict"""

    try:
        return Event(
            event_dict=event_dict,
            room_version=room_version,
            internal_metadata_dict=internal_metadata_dict or {},
            rejected_reason=rejected_reason,
        )
    except ValueError:
        raise SynapseError(400, "Invalid event dict", Codes.BAD_JSON)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _EventRelation:
    # The target event of the relation.
    parent_id: str
    # The relation type.
    rel_type: str
    # The aggregation key. Will be None if the rel_type is not m.annotation or is
    # not a string.
    aggregation_key: str | None


def relation_from_event(event: Event) -> _EventRelation | None:
    """
    Attempt to parse relation information an event.

    Returns:
        The event relation information, if it is valid. None, otherwise.
    """
    relation = event.content.get("m.relates_to")
    if not relation or not isinstance(relation, collections.abc.Mapping):
        # No relation information.
        return None

    # Relations must have a type and parent event ID.
    rel_type = relation.get("rel_type")
    if not isinstance(rel_type, str):
        return None

    parent_id = relation.get("event_id")
    if not isinstance(parent_id, str):
        return None

    # Annotations have a key field.
    aggregation_key = None
    if rel_type == RelationTypes.ANNOTATION:
        aggregation_key = relation.get("key")
        if not isinstance(aggregation_key, str):
            aggregation_key = None

    return _EventRelation(parent_id, rel_type, aggregation_key)


def event_exists_in_state_dag(
    event: Union["EventBase", "EventBuilder", "EventMetadata", "StateKey"],
) -> bool:
    """Given an event, returns true if this event should form part of the state DAG.
    Only valid for room versions which use a state DAG (MSC4242)."""
    state_key = None
    if isinstance(event, EventMetadata):
        state_key = event.state_key
    elif isinstance(event, tuple):  # StateKey
        # can't use StateKey else you get:
        # "Subscripted generics cannot be used with class and instance checks"
        state_key = event[1]
    else:
        state_key = event.state_key if event.is_state() else None

    return state_key is not None


def is_creator(create: EventBase, user_id: str) -> bool:
    """
    Return true if the provided user ID is the room creator.

    This includes additional creators in MSC4289.
    """
    assert create.type == EventTypes.Create
    if create.sender == user_id:
        return True
    if create.room_version.msc4289_creator_power_enabled:
        additional_creators = set(
            create.content.get(EventContentFields.ADDITIONAL_CREATORS, [])
        )
        return user_id in additional_creators
    return False


@attr.s(slots=True, frozen=True, auto_attribs=True)
class StrippedStateEvent:
    """
    A stripped down state event. Usually used for remote invite/knocks so the user can
    make an informed decision on whether they want to join.

    Attributes:
        type: Event `type`
        state_key: Event `state_key`
        sender: Event `sender`
        content: Event `content`
    """

    type: str
    state_key: str
    sender: str
    content: dict[str, Any]


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventMetadata:
    """Returned by `get_metadata_for_events`"""

    room_id: str
    event_type: str
    state_key: str | None
    rejection_reason: str | None
