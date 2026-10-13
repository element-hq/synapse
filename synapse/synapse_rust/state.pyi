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

from typing import Collection, Mapping, Sequence

from twisted.internet.defer import Deferred

from synapse.synapse_rust.database.postgres import ConnectionPool
from synapse.synapse_rust.events import Event
from synapse.types import StateKey, StateMap

class NoChainCoverIndexError(Exception):
    """The room has no chain cover index, so the Rust state resolution cannot
    resolve it. Fall back to the Python implementation."""

class StateResStats:
    """Timings and counts for one resolution. Times are in seconds."""

    total_time: float
    db_time: float
    parse_time: float
    cpu_time: float
    sort_time: float
    auth_time: float
    mainline_time: float
    db_queries: int
    db_rows: int
    db_events: int
    conflicted_events: int
    cache_hit: bool

class StateResolutionHandler:
    """State resolution v2 and v2.1 in Rust, reading the database through the
    native Rust Postgres driver."""

    def resolve(
        self,
        room_id: str,
        room_version: str,
        state_sets: Sequence[StateMap[str]],
        event_map: Mapping[str, Event] | None = None,
    ) -> Deferred[tuple[dict[StateKey, str], StateResStats]]:
        """Resolve the state sets.

        `event_map` holds events that are not persisted yet. It is not
        modified.

        Raises:
            NoChainCoverIndexError: the room has no chain cover index; use the
                Python implementation.
            SynapseError: an event in the conflicted set is malformed.
        """

    def resolve_blocking(
        self,
        room_id: str,
        room_version: str,
        state_sets: Sequence[StateMap[str]],
        event_map: Mapping[str, Event] | None = None,
    ) -> tuple[dict[StateKey, str], StateResStats]:
        """As `resolve`, but blocks and returns the result. For tests, where the
        fake reactor does not wait for a deferred fired from another thread."""

def get_auth_chain_difference_for_test(
    pool: ConnectionPool,
    room_id: str,
    state_sets: Sequence[Collection[str]],
    conflicted: Collection[str] | None = None,
    additional_backwards: Collection[str] | None = None,
) -> tuple[set[str], set[str] | None]:
    """The auth chain difference computed by the Rust store with the chain
    cover index, as `_get_auth_chain_difference_using_cover_index_txn` does.
    For tests."""

def load_events_for_test(
    pool: ConnectionPool, event_ids: Sequence[str]
) -> dict[str, Event]:
    """Events loaded by the Rust store. For tests."""

def resolve_in_memory_for_test(
    room_id: str,
    room_version: str,
    state_sets: Sequence[Mapping[tuple[str, str], str]],
    persisted_events: Mapping[str, Event],
    unpersisted_events: Mapping[str, Event] | None = None,
) -> dict[tuple[str, str], str]:
    """Resolve state with the Rust implementation over an in-memory store of
    `persisted_events`, treating `unpersisted_events` as the caller's event
    map. For tests."""
