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
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine


def run_upgrade(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
    config: HomeServerConfig,
) -> None:
    """
    Part 3 of a multi-step migration to drop the column `user_id` and replace it with
    `full_user_id`. See the database schema docs for more information on the full
    migration steps.
    """
    hostname = config.server.server_name

    if isinstance(database_engine, PostgresEngine):
        # check if the constraint can be validated
        check_sql = """
        SELECT user_id from profiles WHERE full_user_id IS NULL
        """
        cur.execute(check_sql)
        res = cur.fetchall()

        if res:
            # there are rows the background job missed, finish them here before we validate the constraint
            process_rows_sql = """
            UPDATE profiles
            SET full_user_id = '@' || user_id || ?
            WHERE user_id IN (
                SELECT user_id FROM profiles WHERE full_user_id IS NULL
            )
            """
            cur.execute(process_rows_sql, (f":{hostname}",))

        # Now we can validate
        validate_sql = """
        ALTER TABLE profiles VALIDATE CONSTRAINT full_user_id_not_null
        """
        cur.execute(validate_sql)

    else:
        # in SQLite we need to rewrite the table to add the constraint.
        # First drop any temporary table that might be here from a previous failed migration.
        cur.execute("DROP TABLE IF EXISTS temp_profiles")

        create_sql = """
        CREATE TABLE temp_profiles (
            full_user_id text NOT NULL,
            user_id text,
            displayname text,
            avatar_url text,
            UNIQUE (full_user_id),
            UNIQUE (user_id)
        )
        """
        cur.execute(create_sql)

        copy_sql = """
        INSERT INTO temp_profiles (
            user_id,
            displayname,
            avatar_url,
            full_user_id)
            SELECT user_id, displayname, avatar_url, '@' || user_id || ':' || ? FROM profiles
        """
        cur.execute(copy_sql, (f"{hostname}",))

        drop_sql = """
        DROP TABLE profiles
        """
        cur.execute(drop_sql)

        rename_sql = """
        ALTER TABLE temp_profiles RENAME to profiles
        """
        cur.execute(rename_sql)
