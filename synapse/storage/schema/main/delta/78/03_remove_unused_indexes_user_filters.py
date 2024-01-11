#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C
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
from synapse.config.homeserver import HomeServerConfig
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, Sqlite3Engine


def run_update(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
    config: HomeServerConfig,
) -> None:
    """
    Fix to drop unused indexes caused by incorrectly adding UNIQUE constraint to
    columns `user_id` and `full_user_id` of table `user_filters` in previous migration.
    """

    if isinstance(database_engine, Sqlite3Engine):
        cur.execute("DROP TABLE IF EXISTS temp_user_filters")
        create_sql = """
        CREATE TABLE temp_user_filters (
            full_user_id text NOT NULL,
            user_id text NOT NULL,
            filter_id bigint NOT NULL,
            filter_json bytea NOT NULL
        )
        """
        cur.execute(create_sql)

        copy_sql = """
        INSERT INTO temp_user_filters (
            user_id,
            filter_id,
            filter_json,
            full_user_id)
            SELECT user_id, filter_id, filter_json, full_user_id FROM user_filters
        """
        cur.execute(copy_sql)

        drop_sql = """
        DROP TABLE user_filters
        """
        cur.execute(drop_sql)

        rename_sql = """
        ALTER TABLE temp_user_filters RENAME to user_filters
        """
        cur.execute(rename_sql)

        index_sql = """
        CREATE UNIQUE INDEX IF NOT EXISTS user_filters_unique ON
        user_filters (user_id, filter_id)
        """
        cur.execute(index_sql)
