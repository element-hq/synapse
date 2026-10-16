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

"""Simplified sliding sync (MSC4186) in Rust. See `rust/src/sliding_sync/`."""

class SlidingSyncHandler:
    """Computes sliding sync responses in Rust, reading the database through
    the native Postgres driver. Built by `RustHandlers`."""

class SlidingSyncStats:
    """What the Rust implementation did for one request."""

    def __init__(
        self,
        *,
        db_time: float = 0.0,
        checkout_time: float = 0.0,
        transactions: int = 0,
        queries: int = 0,
        rooms_computed: int = 0,
        lazy_member_lookups: int = 0,
    ) -> None: ...
    @property
    def db_time(self) -> float:
        """Time spent waiting on statements, in seconds."""
    @property
    def checkout_time(self) -> float:
        """Time spent waiting for a database connection, in seconds."""
    @property
    def transactions(self) -> int: ...
    @property
    def queries(self) -> int: ...
    @property
    def rooms_computed(self) -> int: ...
    @property
    def lazy_member_lookups(self) -> int: ...
