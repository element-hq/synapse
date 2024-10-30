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
import abc
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Generic, Mapping, Optional, TypeVar

from synapse.storage.types import Connection, Cursor, DBAPI2Module

if TYPE_CHECKING:
    from synapse.storage.database import LoggingDatabaseConnection


# A string that will be replaced with the appropriate auto increment directive
# for the database engine, expands to an auto incrementing integer primary key.
AUTO_INCREMENT_PRIMARY_KEYPLACEHOLDER = "$%AUTO_INCREMENT_PRIMARY_KEY%$"


class IsolationLevel(IntEnum):
    READ_COMMITTED: int = 1
    REPEATABLE_READ: int = 2
    SERIALIZABLE: int = 3


class IncorrectDatabaseSetup(RuntimeError):
    pass


ConnectionType = TypeVar("ConnectionType", bound=Connection)
CursorType = TypeVar("CursorType", bound=Cursor)


class BaseDatabaseEngine(Generic[ConnectionType, CursorType], metaclass=abc.ABCMeta):
    def __init__(self, module: DBAPI2Module, config: Mapping[str, Any]):
        self.module = module

    @property
    @abc.abstractmethod
    def single_threaded(self) -> bool: ...

    @property
    @abc.abstractmethod
    def supports_using_any_list(self) -> bool:
        """
        Do we support using `a = ANY(?)` and passing a list
        """
        ...

    @property
    @abc.abstractmethod
    def supports_returning(self) -> bool:
        """Do we support the `RETURNING` clause in insert/update/delete?"""
        ...

    @abc.abstractmethod
    def check_database(
        self, db_conn: ConnectionType, allow_outdated_version: bool = False
    ) -> None: ...

    @abc.abstractmethod
    def check_new_database(self, txn: CursorType) -> None:
        """Gets called when setting up a brand new database. This allows us to
        apply stricter checks on new databases versus existing database.
        """
        ...

    @abc.abstractmethod
    def convert_param_style(self, sql: str) -> str: ...

    # This method would ideally take a plain ConnectionType, but it seems that
    # the Sqlite engine expects to use LoggingDatabaseConnection.cursor
    # instead of sqlite3.Connection.cursor: only the former takes a txn_name.
    @abc.abstractmethod
    def on_new_connection(self, db_conn: "LoggingDatabaseConnection") -> None: ...

    @abc.abstractmethod
    def is_deadlock(self, error: Exception) -> bool: ...

    @abc.abstractmethod
    def is_connection_closed(self, conn: ConnectionType) -> bool: ...

    @abc.abstractmethod
    def lock_table(self, txn: Cursor, table: str) -> None: ...

    @property
    @abc.abstractmethod
    def server_version(self) -> str:
        """Gets a string giving the server version. For example: '3.22.0'"""
        ...

    @property
    @abc.abstractmethod
    def row_id_name(self) -> str:
        """Gets the literal name representing a row id for this engine."""
        ...

    @abc.abstractmethod
    def in_transaction(self, conn: ConnectionType) -> bool:
        """Whether the connection is currently in a transaction."""
        ...

    @abc.abstractmethod
    def attempt_to_set_autocommit(self, conn: ConnectionType, autocommit: bool) -> None:
        """Attempt to set the connections autocommit mode.

        When True queries are run outside of transactions.

        Note: This has no effect on SQLite3, so callers still need to
        commit/rollback the connections.
        """
        ...

    @abc.abstractmethod
    def attempt_to_set_isolation_level(
        self, conn: ConnectionType, isolation_level: Optional[int]
    ) -> None:
        """Attempt to set the connections isolation level.

        Note: This has no effect on SQLite3, as transactions are SERIALIZABLE by default.
        """
        ...

    @staticmethod
    @abc.abstractmethod
    def executescript(cursor: CursorType, script: str) -> None:
        """Execute a chunk of SQL containing multiple semicolon-delimited statements.

        This is not provided by DBAPI2, and so needs engine-specific support.

        Any ongoing transaction is committed before executing the script in its own
        transaction. The script transaction is left open and it is the responsibility of
        the caller to commit it.
        """
        ...

    @classmethod
    def execute_script_file(cls, cursor: CursorType, filepath: str) -> None:
        """Execute a file containing multiple semicolon-delimited SQL statements.

        This is not provided by DBAPI2, and so needs engine-specific support.
        """
        with open(filepath) as f:
            cls.executescript(cursor, f.read())
