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

from twisted.internet.defer import Deferred

from synapse.storage.database import DatabasePool
from synapse.synapse_rust.caches import RustCache
from synapse.synapse_rust.events import Event
from synapse.synapse_rust.runtime import RustRuntime

class IsFeatureEnabledForUser(RustCache):
    """The Rust `Store::is_feature_enabled_for_user`, and the cache behind it.

    Calling it answers from the cache without leaving the reactor thread, with
    an already-fired Deferred. Only a miss goes to the database, through tokio.
    """

    def __call__(self, user_id: str, feature: str) -> "Deferred[bool]":
        """Whether `feature` is enabled for `user_id`. False when there is no
        row for them.

        Raises ValueError for a feature name the Rust side doesn't know, which
        means `ExperimentalFeature` and `PerUserExperimentalFeature` have
        drifted apart."""

class RustEventCache(RustCache):
    """The event cache, shared by `EventsWorkerStore` and the Rust readers.

    Keys are one-tuples of the event ID. A hit hands back new `Event` objects
    that share the cached event's data, internal metadata and unsigned data
    included, so a change made through one shows through every other.
    """

    def __init__(self, runtime: RustRuntime, max_size: int) -> None:
        """An event cache of its own, for a datastore without a `RustStore`,
        in the runtime's cache registry."""

    def get_local(
        self, key: tuple[str], update_metrics: bool = True
    ) -> tuple[Event, Event | None] | None:
        """The event and its redacted form, if it had a valid redaction when
        it was cached. None if it isn't cached."""

    def touch(self, key: tuple[str], event: Event, update_metrics: bool = True) -> None:
        """Count a hit answered from the caller's own map of live events,
        and mark the entry as read, putting `event` back if it was evicted."""

    def set_local(
        self, key: tuple[str], event: Event, redacted_event: Event | None
    ) -> None:
        """Cache an event, and its redacted form if it has a valid redaction."""

    def invalidate_local(self, key: tuple[str]) -> None:
        """Drop an event, and its chain position."""

    def invalidate_on_extra_index_local(self, index_key: tuple[str]) -> None:
        """Drop a room's events, for a purge, and the store's chain cover and
        state group caches."""

    def clear(self) -> None:
        """Empty the cache, and the store's chain cover and state group caches."""

class RustStore:
    """The Rust side of the main datastore.

    The datastore builds this in its `__init__`, over its own `db_pool`, and
    binds each callable cache here under that cache's name. Get it from
    `hs.get_rust_store()`.
    """

    def __init__(
        self, runtime: RustRuntime, db_pool: DatabasePool, event_cache_size: int
    ) -> None: ...
    @property
    def is_feature_enabled_for_user(self) -> IsFeatureEnabledForUser: ...
    @property
    def loads_events(self) -> bool:
        """Whether `load_events` can be used: whether the database uses the
        native Postgres driver."""

    def load_events(
        self, event_ids: list[str]
    ) -> "Deferred[tuple[dict[str, tuple[Event, Event | None]], int]]":
        """`_get_events_from_db` behind the event cache: the events by ID, each
        with its redacted form if it has a valid redaction, rejected events
        included and unknown ones left out. Each comes from the cache, from a
        load already in flight, or from the database, and what is read from
        the database is cached. Also the number of events this call read from
        the database, which leaves out those of a load it joined.

        May return events that weren't asked for: the redaction events read
        to check an unconfirmed redaction. Cancelling the Deferred leaves the
        load running for the other callers sharing it."""

    def reload_events(
        self, event_ids: list[str]
    ) -> "Deferred[tuple[dict[str, tuple[Event, Event | None]], int]]":
        """`load_events`, reading every event from the database. What it reads
        replaces what was cached."""

    def load_events_blocking(
        self, event_ids: list[str]
    ) -> tuple[dict[str, tuple[Event, Event | None]], int]:
        """`load_events`, blocking. For tests."""

    def reload_events_blocking(
        self, event_ids: list[str]
    ) -> tuple[dict[str, tuple[Event, Event | None]], int]:
        """`reload_events`, blocking. For tests."""
    @property
    def event_cache(self) -> RustEventCache:
        """The event cache, for `EventsWorkerStore` to bind as
        `_get_event_cache`."""

    @property
    def caches(self) -> list[RustCache]:
        """The chain cover and state group caches, to register for metrics
        and cache factors. Nothing invalidates them by name: the event cache
        drops them with the events."""

    @property
    def mirrored_caches(self) -> list[tuple[str, RustCache]]:
        """The caches that mirror a `@cached` function of the datastore, each
        with the function's name: attach each as a mirror of that function's
        cache (`mirror_rust_cache`) and register it for metrics."""
