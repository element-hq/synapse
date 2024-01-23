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

"""
This migration rebuilds the device_lists_outbound_last_success table without duplicate
entries, and with a UNIQUE index.
"""

import logging
from io import StringIO

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine
from synapse.storage.prepare_database import execute_statements_from_stream

logger = logging.getLogger(__name__)


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    # some instances might already have this index, in which case we can skip this
    if isinstance(database_engine, PostgresEngine):
        cur.execute(
            """
            SELECT 1 FROM pg_class WHERE relkind = 'i'
            AND relname = 'device_lists_outbound_last_success_unique_idx'
            """
        )

        if cur.rowcount:
            logger.info(
                "Unique index exists on device_lists_outbound_last_success: "
                "skipping rebuild"
            )
            return

    logger.info("Rebuilding device_lists_outbound_last_success with unique index")
    execute_statements_from_stream(cur, StringIO(_rebuild_commands))


# there might be duplicates, so the easiest way to achieve this is to create a new
# table with the right data, and renaming it into place

_rebuild_commands = """
DROP TABLE IF EXISTS device_lists_outbound_last_success_new;

CREATE TABLE device_lists_outbound_last_success_new (
    destination TEXT NOT NULL,
    user_id TEXT NOT NULL,
    stream_id BIGINT NOT NULL
);

-- this took about 30 seconds on matrix.org's 16 million rows.
INSERT INTO device_lists_outbound_last_success_new
    SELECT destination, user_id, MAX(stream_id) FROM device_lists_outbound_last_success
    GROUP BY destination, user_id;

-- and this another 30 seconds.
CREATE UNIQUE INDEX device_lists_outbound_last_success_unique_idx
    ON device_lists_outbound_last_success_new (destination, user_id);

DROP TABLE device_lists_outbound_last_success;

ALTER TABLE device_lists_outbound_last_success_new
    RENAME TO device_lists_outbound_last_success;
"""
