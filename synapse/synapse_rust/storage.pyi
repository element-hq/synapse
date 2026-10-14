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

class RustStore:
    """The Rust side of the main datastore.

    The datastore builds this in its `__init__`, over its own `db_pool`, and
    binds each callable cache here under that cache's name. Get it from
    `hs.get_rust_store()`.
    """

    def __init__(self, runtime: RustRuntime, db_pool: DatabasePool) -> None: ...
    @property
    def is_feature_enabled_for_user(self) -> IsFeatureEnabledForUser: ...
    @property
    def caches(self) -> list[RustCache]:
        """The event, chain cover and state group caches, to register for
        metrics and cache factors. Nothing invalidates them by name."""

    @property
    def mirrored_caches(self) -> list[tuple[str, RustCache]]:
        """The caches that mirror a `@cached` function of the datastore, each
        with the function's name: attach each as a mirror of that function's
        cache (`mirror_rust_cache`) and register it for metrics."""

    def invalidate_event(self, event_id: str) -> None:
        """Drop an event from the event and chain position caches. Called
        whenever the Python event cache drops it."""

    def clear_event_cache(self) -> None:
        """Empty the event, chain cover and state group caches. Called when a
        room is purged."""
