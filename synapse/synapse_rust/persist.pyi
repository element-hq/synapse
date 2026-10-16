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

from typing import Any

from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.synapse_rust.database.postgres import ConnectionPool

class Persister:
    """Persists events in Rust, on the events writer, over the native driver.
    See `experimental_features.rust_event_persistence`. Get it from
    `hs.get_rust_handlers().persister`."""

    def take_metrics(self) -> dict[str, Any]:
        """What persisting has counted since the last call. See
        `flush_rust_persist_metrics`."""

class PersistEffects:
    """The Python cache updates a Rust persist transaction asks for once it
    has committed, which `DataStore._apply_rust_persist_effects` applies.
    Each attribute matches one kind of `txn.call_after` callback of the
    Python transaction."""

    def __init__(
        self,
        *,
        events: list[
            tuple[int, str, str, str, str | None, str | None, str | None, bool]
        ] = ...,
        event_cache_invalidations: list[str] = ...,
        state_caches: list[tuple[str, list[str]]] = ...,
        cache_invalidations: list[tuple[str, list[str]]] = ...,
        state_group_prefills: list[tuple[str, int]] = ...,
        latest_event_ids: tuple[str, list[str]] | None = None,
        stream_changes: list[tuple[str, str, int]] = ...,
        stream_ids: list[int] = ...,
        backfilled: bool = False,
        new_replication_data: bool = False,
    ) -> None:
        """Build effects in Python, for tests."""

    @property
    def events(
        self,
    ) -> list[tuple[int, str, str, str, str | None, str | None, str | None, bool]]:
        """The arguments of `_invalidate_caches_for_event`, per event:
        stream ordering, event ID, room ID, type, state key, redacts,
        relates to, backfilled."""

    @property
    def event_cache_invalidations(self) -> list[str]:
        """Events to drop from the event cache."""

    @property
    def state_caches(self) -> list[tuple[str, list[str]]]:
        """`_invalidate_state_caches(room_id, members_changed)` calls."""

    @property
    def cache_invalidations(self) -> list[tuple[str, list[str]]]:
        """`(cache name, key)`: the local half of
        `_invalidate_cache_and_stream`."""

    @property
    def state_group_prefills(self) -> list[tuple[str, int]]:
        """`(event ID, state group)` for `_get_state_group_for_event`."""

    @property
    def latest_event_ids(self) -> tuple[str, list[str]] | None:
        """`(room ID, forward extremities)` for
        `get_latest_event_ids_in_room`."""

    @property
    def stream_changes(self) -> list[tuple[str, str, int]]:
        """`(cache, entity, stream ID)`, where cache is "events",
        "current_state_deltas" or "membership"."""

    @property
    def stream_ids(self) -> list[int]:
        """The chunk's stream IDs, still unfinished. Python marks them
        finished after applying everything else, so the current token never
        passes an event the stream change caches don't know about. Positive
        on the backfill stream too, as the ID generator tracks them."""

    @property
    def backfilled(self) -> bool:
        """Whether `stream_ids` are on the backfill stream."""

    @property
    def new_replication_data(self) -> bool:
        """Whether rows were written to the cache invalidation stream."""

class PersistReaderForTest:
    """The persist reads (`rust/src/persist/reader.rs`), each run to
    completion on the tokio runtime over the native driver's pool. The chain
    cover reads each get a connection of their own. For
    `tests/storage/test_persist_reader.py`."""

    def __init__(self, pool: ConnectionPool, persister: Persister) -> None: ...
    def latest_event_ids_in_room(self, room_id: str) -> set[str]: ...
    def state_dag_extremities(self, room_id: str) -> set[str]: ...
    def events_which_are_prevs(self, event_ids: list[str]) -> list[str]: ...
    def prevs_before_rejected(
        self, event_ids: list[str], include_soft_failed: bool
    ) -> set[str]: ...
    def have_seen_events(self, event_ids: list[str]) -> set[str]: ...
    def membership_from_event_ids(
        self, event_ids: list[str]
    ) -> dict[str, tuple[str, str]]:
        """Event ID to `(user_id, membership)`, for membership events only."""
    def is_local_host_in_room_ignoring_users(
        self, room_id: str, ignore_users: list[str]
    ) -> bool: ...
    def room_version_id(self, room_id: str) -> str | None: ...
    def has_sliding_sync_joined_room(self, room_id: str) -> bool: ...
    def last_event_pos_in_room(
        self, room_id: str, event_types: list[str] | None = None
    ) -> tuple[str, str, int] | None:
        """`(event_id, instance_name, stream)`."""
    def soft_failed_sticky_events(
        self, room_id: str, senders: list[str] | None, now_ms: int
    ) -> list[str]: ...
    def has_auth_chain_index(self, room_id: str) -> bool: ...
    def events_with_chain_ids(self, event_ids: list[str]) -> set[str]: ...
    def chains_to_calculate(self, room_ids: list[str]) -> list[tuple[str, str, str]]:
        """`(event_id, type, state_key)` rows."""
    def auth_event_ids(self, event_id: str) -> list[str]: ...
    def auth_event_chains(
        self, event_ids: list[str]
    ) -> list[tuple[str, str, str, int | None, int | None]]:
        """`(event_id, type, state_key, chain_id, sequence_number)` rows."""
    def chain_links(self, chains: list[int]) -> list[tuple[int, int, int, int]]:
        """`(origin_chain_id, origin_sequence_number, target_chain_id,
        target_sequence_number)` rows."""
    def max_sequence_numbers(self, chain_ids: list[int]) -> dict[int, int]: ...
    # The chain cover step (`rust/src/persist/chain_cover.rs`), in a write
    # transaction of its own. Event ID to `(chain_id, sequence_number,
    # links)`.
    def calculate_chain_cover_index_for_events(
        self, room_id: str, events: list[EventBase]
    ) -> dict[str, tuple[int, int, list[tuple[int, int]]]]: ...
    # The extremity calculations (`rust/src/persist/extremities.rs`), each
    # returning its result and what it counted, in the shape
    # `Persister.take_metrics` returns.
    def calculate_new_extremities(
        self,
        events_and_contexts: list[tuple[EventBase, EventContext]],
        latest_event_ids: list[str],
    ) -> tuple[set[str], dict[str, Any]]: ...
    def calculate_new_state_dag_extremities(
        self,
        room_id: str,
        existing: list[str],
        events_and_contexts: list[tuple[EventBase, EventContext]],
    ) -> tuple[set[str], dict[str, Any]]:
        """Raises a 500 `SynapseError` for missing prev state events, as
        Python does."""
    def prune_extremities(
        self,
        room_id: str,
        server_name: str,
        new_latest_event_ids: list[str],
        resolved_state_group: int,
        event_id_to_state_group: dict[str, int],
        events_and_contexts: list[tuple[EventBase, EventContext]],
        now_ms: int,
    ) -> tuple[set[str], dict[str, Any]]: ...
    def get_new_state_after_events(
        self,
        room_id: str,
        server_name: str,
        events_and_contexts: list[tuple[EventBase, EventContext]],
        old_latest_event_ids: list[str],
        new_latest_event_ids: list[str],
        should_prune: bool,
        now_ms: int,
    ) -> tuple[
        dict[tuple[str, str], str] | None,
        dict[tuple[str, str], str] | None,
        set[str],
        dict[str, Any],
    ]:
        """`(current_state, delta_ids, new_latest_event_ids, counts)`."""
    def calculate_state_delta(
        self, room_id: str, current_state: dict[tuple[str, str], str]
    ) -> tuple[list[tuple[str, str]], dict[tuple[str, str], str]]:
        """`(to_delete, to_insert)`."""
    def is_server_still_joined(
        self,
        room_id: str,
        server_name: str,
        events_and_contexts: list[tuple[EventBase, EventContext]],
        to_delete: list[tuple[str, str]],
        to_insert: dict[tuple[str, str], str],
    ) -> bool: ...
    def calculate_new_forward_extremities_and_state_delta(
        self,
        room_id: str,
        server_name: str,
        events_and_contexts: list[tuple[EventBase, EventContext]],
        now_ms: int,
    ) -> tuple[
        set[str] | None,
        tuple[list[tuple[str, str]], dict[tuple[str, str], str], bool] | None,
        dict[str, Any],
    ]:
        """`(new_forward_extremities, delta, counts)`, the delta as
        `(to_delete, to_insert, no_longer_in_room)`."""
    def process_state_dag_forward_extremities_and_state_delta(
        self,
        room_id: str,
        server_name: str,
        events_and_contexts: list[tuple[EventBase, EventContext]],
        now_ms: int,
    ) -> tuple[
        set[str] | None,
        tuple[list[tuple[str, str]], dict[tuple[str, str], str], bool] | None,
        set[str] | None,
        dict[str, Any],
    ]:
        """`(new_forward_extremities, delta, new_state_dag_extremities,
        counts)`."""
    def calculate_sliding_sync_table_changes(
        self,
        room_id: str,
        server_name: str,
        events: list[EventBase],
        to_delete: list[tuple[str, str]],
        to_insert: dict[tuple[str, str], str],
        no_longer_in_room: bool,
    ) -> tuple[
        int | None,
        dict[str, Any],
        dict[str, Any],
        list[tuple[str, str, str, str]],
        list[str],
    ]:
        """`(bump_stamp_to_fully_insert, joined_room_updates,
        membership_snapshot_shared_insert_values,
        to_insert_membership_snapshots, to_delete_membership_snapshots)`,
        each snapshot as `(user_id, sender, membership_event_id,
        membership)`. The snapshots are sorted by user ID."""
    def compute_sticky_events_to_un_soft_fail(
        self,
        room_id: str,
        events: list[EventBase],
        to_delete: list[tuple[str, str]],
        to_insert: dict[tuple[str, str], str],
        no_longer_in_room: bool,
        now_ms: int,
    ) -> set[str]:
        """The soft-failed sticky events that pass auth once the delta is
        applied. Raises a malformed event's auth failure as the auth rules
        raise it."""

def persist_gil_stats() -> dict[str, tuple[int, float]]:
    """`{site: (calls, seconds held)}` for every time event persistence took
    the GIL from tokio."""

def persistence_json_for_test(event: EventBase) -> tuple[str, str]:
    """The `internal_metadata` and `json` columns of `event_json` as Rust
    would write them for `event`."""

def persist_context_for_test(context: EventContext) -> dict[str, Any]:
    """The fields of `context` as the Rust persister reads them."""
