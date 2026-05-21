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
#
"""Type-narrowing helpers for `EventBase`.

`EventBase` subclasses are split by room version (e.g. `FrozenEventV4`,
`FrozenEventVMSC4242`), and certain attributes — such as `prev_state_events`
on MSC4242 events — only exist on a subset of those subclasses. Branching on
`room_version.<flag>` at runtime tells us *which* subclass we have, but the
type checker can't see that link without an `isinstance` cast at every call
site.

This module provides "marker" subclasses of `EventBase` (`MSC4242Event`,
etc.) paired with `TypeIs`-returning predicates (`supports_msc4242_state_dag`,
etc.). A single call to the predicate both performs the room-version check
and narrows the type — replacing the `if room_version.foo: assert
isinstance(event, FrozenEventV...)` idiom.

The marker classes are *type-only*: their metaclass raises on `isinstance`
so they cannot be misused as real runtime classes. Add new markers and
predicates here when a new room-version feature gates access to additional
attributes.
"""

import abc
from typing import (
    TYPE_CHECKING,
    Sequence,
)

from typing_extensions import TypeIs

from synapse.events import EventBase

if TYPE_CHECKING:
    from synapse.events.snapshot import EventContext, EventPersistencePair


class _DisableIsInstance(abc.ABCMeta):
    """Metaclass which disables isinstance checks on classes which use it, by
    making isinstance() raise NotImplementedError.

    This is used to prevent isinstance checks on EventProtocol, which is a
    helper class used for type narrowing of EventBase objects, but which should
    not be used for isinstance checks itself (as its purely type annotation
    rather than a real class).
    """

    def __instancecheck__(cls, instance: object) -> bool:
        raise NotImplementedError("Instance cannot be used.")


class EventProtocol(EventBase, metaclass=_DisableIsInstance):
    """Helper subclass that allows type narrowing for `EventBase` objects."""


class MSC4242Event(EventProtocol):
    """Marker protocol for events in MSC4242 rooms. This allows us to narrow the
    type of events."""

    prev_state_events: list[str]


def supports_msc4242_state_dag(event: EventBase) -> TypeIs[MSC4242Event]:
    """Returns true if the given event is in a room that supports state DAGs
    (MSC4242)"""

    return event.room_version.msc4242_state_dags


def all_supports_msc4242_state_dag(
    obj: Sequence["EventPersistencePair"],
) -> TypeIs[Sequence[tuple[MSC4242Event, "EventContext"]]]:
    """Returns true if the given sequence of events are all in a room that
    supports state DAGs (MSC4242)"""

    return all(event.room_version.msc4242_state_dags for event, _ in obj)
