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

def persist_gil_stats() -> dict[str, tuple[int, float]]:
    """`{site: (calls, seconds held)}` for every time event persistence took
    the GIL from tokio."""

def persistence_json_for_test(event: EventBase) -> tuple[str, str]:
    """The `internal_metadata` and `json` columns of `event_json` as Rust
    would write them for `event`."""

def persist_context_for_test(context: EventContext) -> dict[str, Any]:
    """The fields of `context` as the Rust persister reads them."""
