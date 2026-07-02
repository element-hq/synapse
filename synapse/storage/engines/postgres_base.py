#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import logging
from typing import Any, Mapping, TypeVar, cast

from synapse.storage.engines._base import BaseDatabaseEngine, IncorrectDatabaseSetup
from synapse.storage.types import Connection, Cursor, DBAPI2Module
from synapse.util.duration import Duration

logger = logging.getLogger(__name__)

ConnectionType = TypeVar("ConnectionType", bound=Connection)
CursorType = TypeVar("CursorType", bound=Cursor)


class PostgresEngine(BaseDatabaseEngine[ConnectionType, CursorType]):
    """Behaviour shared by the Postgres backends, regardless of driver.

    This holds the SQL-dialect and configuration logic that is identical whether
    Synapse talks to Postgres via psycopg2 (:class:`Psycopg2Engine`) or the
    native Rust driver (:class:`RustPostgresEngine`). Everything that touches a
    live connection, the DBAPI2 exception module, or the parameter-placeholder
    style is left abstract for those subclasses to provide.

    Crucially the name is kept as ``PostgresEngine`` so the many
    ``isinstance(engine, PostgresEngine)`` checks across the storage layer — all
    of which mean "emit Postgres SQL" — hold for both drivers.
    """

    # Whether `LoggingTransaction` may use the `psycopg2.extras` helpers on the
    # cursor. Set by each concrete subclass (True for psycopg2, False for Rust).
    uses_psycopg2_extras: bool

    def __init__(self, module: DBAPI2Module, database_config: Mapping[str, Any]):
        super().__init__(module, database_config)

        self.synchronous_commit: bool = database_config.get("synchronous_commit", True)
        # Set the statement timeout to 10 minutes by default.
        #
        # Any query taking more than 10 minutes should probably be considered a bug;
        # most of the time this is a sign that work needs to be split up or that
        # some degenerate query plan has been created and the client has probably
        # timed out/walked off anyway.
        # This is in milliseconds.
        self.statement_timeout: int | None = database_config.get(
            "statement_timeout", Duration(minutes=10).as_millis()
        )
        self.config = database_config

    @property
    def single_threaded(self) -> bool:
        return False

    @property
    def supports_using_any_list(self) -> bool:
        """Do we support using `a = ANY(?)` and passing a list"""
        return True

    @property
    def row_id_name(self) -> str:
        return "ctid"

    def get_db_locale(self, txn: Cursor) -> tuple[str, str]:
        txn.execute(
            "SELECT datcollate, datctype FROM pg_database WHERE datname = current_database()"
        )
        collation, ctype = cast(tuple[str, str], txn.fetchone())
        return collation, ctype

    def check_new_database(self, txn: Cursor) -> None:
        """Gets called when setting up a brand new database. This allows us to
        apply stricter checks on new databases versus existing database.
        """

        allow_unsafe_locale = self.config.get("allow_unsafe_locale", False)
        if allow_unsafe_locale:
            return

        collation, ctype = self.get_db_locale(txn)

        errors = []

        if collation != "C":
            errors.append("    - 'COLLATE' is set to %r. Should be 'C'" % (collation,))

        if ctype != "C":
            errors.append("    - 'CTYPE' is set to %r. Should be 'C'" % (ctype,))

        if errors:
            raise IncorrectDatabaseSetup(
                "Database is incorrectly configured:\n\n%s\n\n"
                "See docs/postgres.md for more information. You can override this check by"
                "setting 'allow_unsafe_locale' to true in the database config.",
                "\n".join(errors),
            )

    def lock_table(self, txn: Cursor, table: str) -> None:
        txn.execute("LOCK TABLE %s in EXCLUSIVE MODE" % (table,))
