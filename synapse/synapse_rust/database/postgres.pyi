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

from types import TracebackType
from typing import Any, Optional, Sequence

from synapse.synapse_rust.runtime import RustRuntime

class Connection:
    """A native Postgres connection, transactional by default like psycopg2."""

    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...
    def set_autocommit(self, autocommit: bool) -> None: ...
    def __enter__(self) -> Connection: ...
    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> bool: ...

class Cursor:
    """A PEP-249-style cursor."""

    def execute(self, query: str, params: Optional[Sequence[Any]] = None) -> None: ...
    def fetch_one(self) -> Optional[tuple[Any, ...]]: ...
    def fetch_all(self) -> list[tuple[Any, ...]]: ...
    def fetch_next_batch(self, capacity: int = 100) -> list[tuple[Any, ...]]: ...
    @property
    def rowcount(self) -> int: ...
    def close(self) -> None: ...

def connect(runtime: RustRuntime, dsn: str) -> Connection:
    """Open a Postgres connection from a libpq-style DSN, blocking until done."""
