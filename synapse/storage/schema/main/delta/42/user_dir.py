#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine
from synapse.storage.prepare_database import get_statements

logger = logging.getLogger(__name__)


BOTH_TABLES = """
CREATE TABLE user_directory_stream_pos (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    stream_id BIGINT,
    CHECK (Lock='X')
);

INSERT INTO user_directory_stream_pos (stream_id) VALUES (null);

CREATE TABLE user_directory (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,  -- A room_id that we know the user is joined to
    display_name TEXT,
    avatar_url TEXT
);

CREATE INDEX user_directory_room_idx ON user_directory(room_id);
CREATE UNIQUE INDEX user_directory_user_idx ON user_directory(user_id);

CREATE TABLE users_in_pubic_room (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL  -- A room_id that we know is public
);

CREATE INDEX users_in_pubic_room_room_idx ON users_in_pubic_room(room_id);
CREATE UNIQUE INDEX users_in_pubic_room_user_idx ON users_in_pubic_room(user_id);
"""


POSTGRES_TABLE = """
CREATE TABLE user_directory_search (
    user_id TEXT NOT NULL,
    vector tsvector
);

CREATE INDEX user_directory_search_fts_idx ON user_directory_search USING gin(vector);
CREATE UNIQUE INDEX user_directory_search_user_idx ON user_directory_search(user_id);
"""


SQLITE_TABLE = """
CREATE VIRTUAL TABLE user_directory_search
    USING fts4 ( user_id, value );
"""


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    for statement in get_statements(BOTH_TABLES.splitlines()):
        cur.execute(statement)

    if isinstance(database_engine, PostgresEngine):
        for statement in get_statements(POSTGRES_TABLE.splitlines()):
            cur.execute(statement)
    elif isinstance(database_engine, Sqlite3Engine):
        for statement in get_statements(SQLITE_TABLE.splitlines()):
            cur.execute(statement)
    else:
        raise Exception("Unrecognized database engine")
