#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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


# Change the last_token to last_stream_ordering now that pushers no longer
# listen on an event stream but instead select out of the event_push_actions
# table.


import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine

logger = logging.getLogger(__name__)


def token_to_stream_ordering(token: str) -> int:
    return int(token[1:].split("_")[0])


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    logger.info("Porting pushers table, delta 31...")
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
          last_stream_ordering INTEGER,
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
        row[12] = token_to_stream_ordering(row[12])
        cur.execute(
            """
                INSERT into pushers2 (
                id, user_name, access_token, profile_tag, kind,
                app_id, app_display_name, device_display_name,
                pushkey, ts, lang, data, last_stream_ordering, last_success,
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
