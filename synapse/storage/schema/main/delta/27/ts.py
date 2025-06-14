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
from synapse.storage.engines import BaseDatabaseEngine
from synapse.storage.prepare_database import get_statements

logger = logging.getLogger(__name__)


ALTER_TABLE = (
    "ALTER TABLE events ADD COLUMN origin_server_ts BIGINT;"
    "CREATE INDEX events_ts ON events(origin_server_ts, stream_ordering);"
)


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    for statement in get_statements(ALTER_TABLE.splitlines()):
        cur.execute(statement)

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

        cur.execute(sql, ("event_origin_server_ts", progress_json))
