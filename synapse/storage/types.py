#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from types import TracebackType
from typing import (
    Any,
    Callable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
)

from typing_extensions import Protocol

"""
Some very basic protocol definitions for the DB-API2 classes specified in PEP-249
"""

SQLQueryParameters = Union[Sequence[Any], Mapping[str, Any]]


class Cursor(Protocol):
    def execute(self, sql: str, parameters: SQLQueryParameters = ...) -> Any: ...

    def executemany(
        self, sql: str, parameters: Sequence[SQLQueryParameters]
    ) -> Any: ...

    def fetchone(self) -> Optional[Tuple]: ...

    def fetchmany(self, size: Optional[int] = ...) -> List[Tuple]: ...

    def fetchall(self) -> List[Tuple]: ...

    @property
    def description(
        self,
    ) -> Optional[Sequence[Any]]:
        # At the time of writing, Synapse only assumes that `column[0]: str` for each
        # `column in description`. Since this is hard to express in the type system, and
        # as this is rarely used in Synapse, we deem `column: Any` good enough.
        ...

    @property
    def rowcount(self) -> int:
        return 0

    def __iter__(self) -> Iterator[Tuple]: ...

    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def close(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __enter__(self) -> "Connection": ...

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> Optional[bool]: ...


class DBAPI2Module(Protocol):
    """The module-level attributes that we use from PEP 249.

    This is NOT a comprehensive stub for the entire DBAPI2."""

    __name__: str

    # Exceptions. See https://peps.python.org/pep-0249/#exceptions

    # For our specific drivers:
    # - Python's sqlite3 module doesn't contains the same descriptions as the
    #   DBAPI2 spec, see https://docs.python.org/3/library/sqlite3.html#exceptions
    # - Psycopg2 maps every Postgres error code onto a unique exception class which
    #   extends from this hierarchy. See
    #     https://docs.python.org/3/library/sqlite3.html?highlight=sqlite3#exceptions
    #     https://www.postgresql.org/docs/current/errcodes-appendix.html#ERRCODES-TABLE
    #
    # Note: rather than
    #     x: T
    # we write
    #     @property
    #     def x(self) -> T: ...
    # which expresses that the protocol attribute `x` is read-only. The mypy docs
    #     https://mypy.readthedocs.io/en/latest/common_issues.html#covariant-subtyping-of-mutable-protocol-members-is-rejected
    # explain why this is necessary for safety. TL;DR: we shouldn't be able to write
    # to `x`, only read from it. See also https://github.com/python/mypy/issues/6002 .
    @property
    def Warning(self) -> Type[Exception]: ...

    @property
    def Error(self) -> Type[Exception]: ...

    # Errors are divided into `InterfaceError`s (something went wrong in the database
    # driver) and `DatabaseError`s (something went wrong in the database). These are
    # both subclasses of `Error`, but we can't currently express this in type
    # annotations due to https://github.com/python/mypy/issues/8397
    @property
    def InterfaceError(self) -> Type[Exception]: ...

    @property
    def DatabaseError(self) -> Type[Exception]: ...

    # Everything below is a subclass of `DatabaseError`.

    # Roughly: the database rejected a nonsensical value. Examples:
    # - An integer was too big for its data type.
    # - An invalid date time was provided.
    # - A string contained a null code point.
    @property
    def DataError(self) -> Type[Exception]: ...

    # Roughly: something went wrong in the database, but it's not within the application
    # programmer's control. Examples:
    # - We failed to establish a connection to the database.
    # - The connection to the database was lost.
    # - A deadlock was detected.
    # - A serialisation failure occurred.
    # - The database ran out of resources, such as storage, memory, connections, etc.
    # - The database encountered an error from the operating system.
    @property
    def OperationalError(self) -> Type[Exception]: ...

    # Roughly: we've given the database data which breaks a rule we asked it to enforce.
    # Examples:
    # - Stop, criminal scum! You violated the foreign key constraint
    # - Also check constraints, non-null constraints, etc.
    @property
    def IntegrityError(self) -> Type[Exception]: ...

    # Roughly: something went wrong within the database server itself.
    @property
    def InternalError(self) -> Type[Exception]: ...

    # Roughly: the application did something silly that needs to be fixed. Examples:
    # - We don't have permissions to do something.
    # - We tried to create a table with duplicate column names.
    # - We tried to use a reserved name.
    # - We referred to a column that doesn't exist.
    @property
    def ProgrammingError(self) -> Type[Exception]: ...

    # Roughly: we've tried to do something that this database doesn't support.
    @property
    def NotSupportedError(self) -> Type[Exception]: ...

    # We originally wrote
    # def connect(self, *args, **kwargs) -> Connection: ...
    # But mypy doesn't seem to like that because sqlite3.connect takes a mandatory
    # positional argument. We can't make that part of the signature though, because
    # psycopg2.connect doesn't have a mandatory positional argument. Instead, we use
    # the following slightly unusual workaround.
    @property
    def connect(self) -> Callable[..., Connection]: ...


__all__ = ["Cursor", "Connection", "DBAPI2Module"]
