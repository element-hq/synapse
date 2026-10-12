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

class RustCache:
    """A cache that lives in Rust, as Synapse sees it.

    The datastore binds one under the cache's name and passes it to
    `synapse.util.caches.register_rust_cache`. It implements the parts of
    `CachedFunction` that Synapse's invalidation machinery calls, so
    `_attempt_to_invalidate_cache` and `_invalidate_cache_and_stream` work on it
    unchanged. A cache Python also reads through is a callable subclass: see
    `synapse.synapse_rust.storage`.
    """

    __name__: str

    def invalidate(self, keys: tuple[Any, ...]) -> None: ...
    def invalidate_all(self) -> None: ...
    def __len__(self) -> int: ...
    def set_cache_factor(self, factor: float) -> None: ...
    @property
    def max_size(self) -> int: ...
    @property
    def hits(self) -> int: ...
    @property
    def misses(self) -> int: ...
    @property
    def evicted_size(self) -> int: ...
    @property
    def evicted_time(self) -> int: ...
    @property
    def evicted_invalidation(self) -> int: ...
