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
from typing import Any, Mapping, NoReturn

from ._base import BaseDatabaseEngine, IncorrectDatabaseSetup

# `PostgresEngine` is the driver-agnostic Postgres base (psycopg2 and the native
# Rust backend both subclass it). It has no driver dependency, so it always
# imports — which matters because `isinstance(engine, PostgresEngine)` is used
# throughout the storage layer to write Postgres- vs sqlite-flavoured queries.
from .postgres_base import PostgresEngine

# The concrete driver engines are optional: their driver modules may not be
# installed. Create dummy classes on import failure so `isinstance()` checks
# still work (and construction fails with a clear message).
try:
    from .postgres import Psycopg2Engine
except ImportError:

    class Psycopg2Engine(PostgresEngine):  # type: ignore[no-redef]
        def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
            raise RuntimeError(
                f"Cannot create {cls.__name__} -- psycopg2 module is not installed"
            )


try:
    from .sqlite import Sqlite3Engine
except ImportError:

    class Sqlite3Engine(BaseDatabaseEngine):  # type: ignore[no-redef]
        def __new__(cls, *args: object, **kwargs: object) -> NoReturn:
            raise RuntimeError(
                f"Cannot create {cls.__name__} -- sqlite3 module is not installed"
            )


def create_engine(database_config: Mapping[str, Any]) -> BaseDatabaseEngine:
    name = database_config["name"]

    if name == "sqlite3":
        return Sqlite3Engine(database_config)

    if name == "psycopg2":
        return Psycopg2Engine(database_config)

    raise RuntimeError("Unsupported database engine '%s'" % (name,))


__all__ = [
    "create_engine",
    "BaseDatabaseEngine",
    "PostgresEngine",
    "Psycopg2Engine",
    "Sqlite3Engine",
    "IncorrectDatabaseSetup",
]
