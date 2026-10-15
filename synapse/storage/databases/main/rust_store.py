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

from typing import TYPE_CHECKING

from synapse.storage._base import SQLBaseStore
from synapse.storage.database import DatabasePool, LoggingDatabaseConnection
from synapse.synapse_rust.storage import RustStore
from synapse.util.caches import mirror_rust_cache, register_rust_cache
from synapse.util.caches.descriptors import CachedFunction

if TYPE_CHECKING:
    from synapse.server import HomeServer


class RustWorkerStore(SQLBaseStore):
    """The Rust side of the datastore, over the same database pool.

    The Rust handlers share it, via `hs.get_rust_store()`. Its caches are
    registered here for metrics and cache factors like any other, and those
    that mirror a `@cached` function of this datastore are attached to that
    function's cache, so that every invalidation of the Python cache reaches
    the Rust one too.
    """

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ) -> None:
        super().__init__(database, db_conn, hs)

        self.get_or_build_rust_store(hs)

        # The chain cover and state group caches are emptied with the event
        # cache, which `EventsWorkerStore` binds and registers, so they are
        # registered for metrics and cache factors only.
        for cache in self.rust_store.caches:
            register_rust_cache(cache, server_name=hs.hostname)

        for python_name, rust_cache in self.rust_store.mirrored_caches:
            cached_function: CachedFunction = getattr(self, python_name)
            assert hasattr(cached_function, "cache"), f"{python_name} is not @cached"
            mirror_rust_cache(cached_function, rust_cache)
            register_rust_cache(rust_cache, server_name=hs.hostname)

    def get_or_build_rust_store(self, hs: "HomeServer") -> RustStore:
        """The Rust store, built on the first call.

        `EventsWorkerStore.__init__` needs the store too, to bind its event
        cache. Whether that runs before or after this class's `__init__`
        depends on where each falls in the datastore's MRO, which differs
        between datastores, so whichever runs first builds it.
        """
        rust_store: RustStore | None = getattr(self, "rust_store", None)
        if rust_store is None:
            rust_store = RustStore(
                hs.get_rust_runtime(),
                self.db_pool,
                hs.config.caches.event_cache_size,
            )
            self.rust_store = rust_store
        return rust_store
