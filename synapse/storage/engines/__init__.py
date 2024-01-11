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

# The classes `PostgresEngine` and `Sqlite3Engine` must always be importable, because
# we use `isinstance(engine, PostgresEngine)` to write different queries for postgres
# and sqlite. But the database driver modules are both optional: they may not be
# installed. To account for this, create dummy classes on import failure so we can
# still run `isinstance()` checks.
try:
    from .postgres import PostgresEngine
except ImportError:

    class PostgresEngine(BaseDatabaseEngine):  # type: ignore[no-redef]
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
        return PostgresEngine(database_config)

    raise RuntimeError("Unsupported database engine '%s'" % (name,))


__all__ = [
    "create_engine",
    "BaseDatabaseEngine",
    "PostgresEngine",
    "Sqlite3Engine",
    "IncorrectDatabaseSetup",
]
