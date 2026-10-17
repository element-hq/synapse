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

"""Applying what a Rust persist transaction asks for once it commits."""

from collections.abc import Mapping
from typing import TYPE_CHECKING

from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.synapse_rust.persist import PersistEffects
from synapse.util.caches.stream_change_cache import StreamChangeCache

if TYPE_CHECKING:
    from synapse.events import EventBase
    from synapse.storage.databases.main import DataStore


def apply_rust_persist_effects(
    store: "DataStore",
    effects: PersistEffects,
    events: Mapping[str, "EventBase"] | None = None,
    *,
    finish_stream_ids: bool = True,
) -> None:
    """Apply what a Rust persist transaction asks for once it has
    committed: the cache updates the Python transaction queues with
    `txn.call_after`. See `synapse_rust.persist.PersistEffects`.
    `store` is the main store of the event persister. On a worker that is a
    `GenericWorkerStore`, which has every attribute used here.
    `events` must hold the events `effects.event_cache_prefills` names.

    Invalidations come before prefills, so a prefill is not lost to an
    invalidation of the same key. The cache, profile updates and sticky
    events stream IDs are finished once the caches are invalidated, as
    `get_next_txn` and `get_next_mult_txn` finish them after the
    transaction's earlier callbacks. The events stream IDs are finished last, as Python's
    `get_next_mult` does once the transaction's callbacks have run, so
    the current token never passes an event before the stream change
    caches know about it. Then `stream_positions` moves on, as
    `get_next_mult` moves it on success.

    If a step raises, the IDs of all four streams are still finished, as
    `call_on_exception` and `get_next_mult` finish them in Python, so the
    streams do not stall.

    Without `finish_stream_ids`, the events stream IDs are left to the
    caller, who must call `finish_rust_persist_stream_ids` whatever
    happens.
    """
    cache_ids_finished = profile_ids_finished = sticky_ids_finished = False
    # Left unfinished on purpose, without `finish_stream_ids`.
    event_ids_finished = not finish_stream_ids
    try:
        for (
            stream_ordering,
            event_id,
            room_id,
            etype,
            state_key,
            redacts,
            relates_to,
            backfilled,
        ) in effects.events:
            store._invalidate_caches_for_event(
                stream_ordering,
                event_id,
                room_id,
                etype,
                state_key,
                redacts,
                relates_to,
                backfilled=backfilled,
            )

        for event_id in effects.event_cache_invalidations:
            store._invalidate_local_get_event_cache(event_id)

        for room_id, members_changed in effects.state_caches:
            store._invalidate_state_caches(room_id, members_changed)

        for cache_name, keys in effects.cache_invalidations:
            store._attempt_to_invalidate_cache(cache_name, tuple(keys))
        if effects.cache_stream_ids:
            assert store._cache_id_gen is not None
            store._cache_id_gen._mark_ids_as_finished(effects.cache_stream_ids)
            cache_ids_finished = True
            store.hs.get_replication_notifier().notify_replication()

        stream_change_caches: dict[str, StreamChangeCache] = {
            "events": store._events_stream_cache,
            "current_state_deltas": store._curr_state_delta_stream_cache,
            "membership": store._membership_stream_cache,
        }
        for cache, entity, stream_id in effects.stream_changes:
            stream_change_caches[cache].entity_has_changed(entity, stream_id)

        if effects.profile_stream_ids:
            _finish_txn_stream_ids(
                store, store._profile_updates_id_gen, effects.profile_stream_ids
            )
            profile_ids_finished = True
        if effects.sticky_stream_ids:
            _finish_txn_stream_ids(
                store, store._sticky_events_id_gen, effects.sticky_stream_ids
            )
            sticky_ids_finished = True

        for event_id in effects.event_cache_prefills:
            assert events is not None
            store._get_event_cache.set_local((event_id,), events[event_id], None)

        for event_id, state_group in effects.state_group_prefills:
            store._get_state_group_for_event.prefill((event_id,), state_group)

        if effects.latest_event_ids is not None:
            room_id, extremities = effects.latest_event_ids
            store.get_latest_event_ids_in_room.prefill(
                (room_id,), frozenset(extremities)
            )

        if not event_ids_finished:
            finish_rust_persist_stream_ids(store, effects, succeeded=True)
            event_ids_finished = True
        if effects.new_replication_data:
            store.hs.get_notifier().on_new_replication_data()
    except BaseException:
        if effects.cache_stream_ids and not cache_ids_finished:
            assert store._cache_id_gen is not None
            store._cache_id_gen._mark_ids_as_finished(effects.cache_stream_ids)
        if effects.profile_stream_ids and not profile_ids_finished:
            store._profile_updates_id_gen._mark_ids_as_finished(
                effects.profile_stream_ids
            )
        if effects.sticky_stream_ids and not sticky_ids_finished:
            store._sticky_events_id_gen._mark_ids_as_finished(effects.sticky_stream_ids)
        if not event_ids_finished:
            finish_rust_persist_stream_ids(store, effects, succeeded=False)
        raise


def finish_rust_persist_stream_ids(
    store: "DataStore", effects: PersistEffects, *, succeeded: bool
) -> None:
    """Finish the events or backfill stream IDs a Rust persist took, as
    `get_next_mult` finishes them when its block exits, and tell
    replication. Only on success does `stream_positions` move on."""
    if not effects.stream_ids:
        return
    id_gen = store._backfill_id_gen if effects.backfilled else store._stream_id_gen
    if succeeded:
        _finish_txn_stream_ids(store, id_gen, effects.stream_ids)
    else:
        id_gen._mark_ids_as_finished(effects.stream_ids)
        store.hs.get_replication_notifier().notify_replication()


def _finish_txn_stream_ids(
    store: "DataStore", id_gen: MultiWriterIdGenerator, stream_ids: list[int]
) -> None:
    """What `get_next_mult_txn` queues once its transaction commits: finish
    the IDs, tell replication, and move `stream_positions` on."""
    id_gen._mark_ids_as_finished(stream_ids)
    store.hs.get_replication_notifier().notify_replication()
    if id_gen._writers:
        store.hs.run_as_background_process(
            "MultiWriterIdGenerator._update_table",
            id_gen._db.runInteraction,
            "MultiWriterIdGenerator._update_table",
            id_gen._update_stream_positions_table_txn,
        )
