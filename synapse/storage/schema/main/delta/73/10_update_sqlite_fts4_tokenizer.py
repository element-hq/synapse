#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, Sqlite3Engine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    """
    Upgrade the event_search table to use the porter tokenizer if it isn't already

    Applies only for sqlite.
    """
    if not isinstance(database_engine, Sqlite3Engine):
        return

    # Rebuild the table event_search table with tokenize=porter configured.
    cur.execute("DROP TABLE event_search")
    cur.execute(
        """
        CREATE VIRTUAL TABLE event_search
        USING fts4 (tokenize=porter, event_id, room_id, sender, key, value )
        """
    )

    # Re-run the background job to re-populate the event_search table.
    cur.execute("SELECT MIN(stream_ordering) FROM events")
    row = cur.fetchone()
    assert row is not None
    min_stream_id = row[0]

    # If there are not any events, nothing to do.
    if min_stream_id is None:
        return

    cur.execute("SELECT MAX(stream_ordering) FROM events")
    row = cur.fetchone()
    assert row is not None
    max_stream_id = row[0]

    progress = {
        "target_min_stream_id_inclusive": min_stream_id,
        "max_stream_id_exclusive": max_stream_id + 1,
    }
    progress_json = json.dumps(progress)

    sql = """
    INSERT into background_updates (ordering, update_name, progress_json)
    VALUES (?, ?, ?)
    """

    cur.execute(sql, (7310, "event_search", progress_json))
