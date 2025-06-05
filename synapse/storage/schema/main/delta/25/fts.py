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
import json
import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine
from synapse.storage.prepare_database import get_statements

logger = logging.getLogger(__name__)


POSTGRES_TABLE = """
CREATE TABLE IF NOT EXISTS event_search (
    event_id TEXT,
    room_id TEXT,
    sender TEXT,
    key TEXT,
    vector tsvector
);

CREATE INDEX event_search_fts_idx ON event_search USING gin(vector);
CREATE INDEX event_search_ev_idx ON event_search(event_id);
CREATE INDEX event_search_ev_ridx ON event_search(room_id);
"""


SQLITE_TABLE = (
    "CREATE VIRTUAL TABLE event_search"
    " USING fts4 ( event_id, room_id, sender, key, value )"
)


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if isinstance(database_engine, PostgresEngine):
        for statement in get_statements(POSTGRES_TABLE.splitlines()):
            cur.execute(statement)
    elif isinstance(database_engine, Sqlite3Engine):
        cur.execute(SQLITE_TABLE)
    else:
        raise Exception("Unrecognized database engine")

    cur.execute("SELECT MIN(stream_ordering) FROM events")
    rows = cur.fetchall()
    min_stream_id = rows[0][0]

    cur.execute("SELECT MAX(stream_ordering) FROM events")
    rows = cur.fetchall()
    max_stream_id = rows[0][0]

    if min_stream_id is not None and max_stream_id is not None:
        progress = {
            "target_min_stream_id_inclusive": min_stream_id,
            "max_stream_id_exclusive": max_stream_id + 1,
            "rows_inserted": 0,
        }
        progress_json = json.dumps(progress)

        sql = (
            "INSERT into background_updates (update_name, progress_json) VALUES (?, ?)"
        )

        cur.execute(sql, ("event_search", progress_json))
