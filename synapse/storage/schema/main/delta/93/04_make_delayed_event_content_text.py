#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine

logger = logging.getLogger(__name__)


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    """
    Change the type / affinity of the `delayed_events` table's `content` column from bytes to text.
    This brings it in line with the `event_json` table's `json` column, and fixes the inability to
    schedule a delayed event with non-ASCII characters in its content.
    """

    if isinstance(database_engine, PostgresEngine):
        cur.execute(
            "ALTER TABLE delayed_events "
            "ALTER COLUMN content SET DATA TYPE TEXT "
            "USING convert_from(content, 'utf8')"
        )
        return

    # For sqlite3, change the type affinity by fiddling with the table schema directly.
    # This strategy is also used by ../50/make_event_content_nullable.py.

    cur.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='delayed_events' AND type='table'"
    )
    row = cur.fetchone()
    assert row is not None
    (oldsql,) = row

    sql = oldsql.replace("content bytea", "content TEXT")
    if sql == oldsql:
        raise Exception("Couldn't find content bytes column in %s" % oldsql)

    cur.execute("PRAGMA schema_version")
    row = cur.fetchone()
    assert row is not None
    (oldver,) = row
    cur.execute("PRAGMA writable_schema=ON")
    cur.execute(
        "UPDATE sqlite_master SET sql=? WHERE tbl_name='delayed_events' AND type='table'",
        (sql,),
    )
    cur.execute("PRAGMA schema_version=%i" % (oldver + 1,))
    cur.execute("PRAGMA writable_schema=OFF")
