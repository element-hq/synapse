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


"""
Main purpose of this upgrade is to change the unique key on the
pushers table again (it was missed when the v16 full schema was
made) but this also changes the pushkey and data columns to text.
When selecting a bytea column into a text column, postgres inserts
the hex encoded data, and there's no portable way of getting the
UTF-8 bytes, so we have to do it in Python.
"""

import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine

logger = logging.getLogger(__name__)


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    logger.info("Porting pushers table...")
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS pushers2 (
          id BIGINT PRIMARY KEY,
          user_name TEXT NOT NULL,
          access_token BIGINT DEFAULT NULL,
          profile_tag VARCHAR(32) NOT NULL,
          kind VARCHAR(8) NOT NULL,
          app_id VARCHAR(64) NOT NULL,
          app_display_name VARCHAR(64) NOT NULL,
          device_display_name VARCHAR(128) NOT NULL,
          pushkey TEXT NOT NULL,
          ts BIGINT NOT NULL,
          lang VARCHAR(8),
          data TEXT,
          last_token TEXT,
          last_success BIGINT,
          failing_since BIGINT,
          UNIQUE (app_id, pushkey, user_name)
        )
    """
    )
    cur.execute(
        """SELECT
        id, user_name, access_token, profile_tag, kind,
        app_id, app_display_name, device_display_name,
        pushkey, ts, lang, data, last_token, last_success,
        failing_since
        FROM pushers
    """
    )
    count = 0
    for tuple_row in cur.fetchall():
        row = list(tuple_row)
        row[8] = bytes(row[8]).decode("utf-8")
        row[11] = bytes(row[11]).decode("utf-8")
        cur.execute(
            """
                INSERT into pushers2 (
                id, user_name, access_token, profile_tag, kind,
                app_id, app_display_name, device_display_name,
                pushkey, ts, lang, data, last_token, last_success,
                failing_since
                ) values (%s)
            """
            % (",".join(["?" for _ in range(len(row))])),
            row,
        )
        count += 1
    cur.execute("DROP TABLE pushers")
    cur.execute("ALTER TABLE pushers2 RENAME TO pushers")
    logger.info("Moved %d pushers to new table", count)
