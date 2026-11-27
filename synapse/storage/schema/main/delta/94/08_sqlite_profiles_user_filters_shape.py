#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
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

# The shape delta 78 was trying to leave these two tables in, plus the columns added
# since. Both code paths through `prepare_database` end up here, so that a server that
# upgraded into delta 78 and one that installed after it agree.
_CREATE_TEMP_PROFILES = """
CREATE TABLE temp_profiles (
    full_user_id text NOT NULL,
    user_id text,
    displayname text,
    avatar_url text,
    fields JSONB,
    UNIQUE (full_user_id),
    UNIQUE (user_id)
)
"""

_CREATE_TEMP_USER_FILTERS = """
CREATE TABLE temp_user_filters (
    full_user_id text NOT NULL,
    user_id text NOT NULL,
    filter_id bigint NOT NULL,
    filter_json bytea NOT NULL
)
"""


def _has_null_full_user_ids(cur: LoggingTransaction, table: str) -> bool:
    cur.execute(f"SELECT 1 FROM {table} WHERE full_user_id IS NULL LIMIT 1")
    return cur.fetchone() is not None


def run_create(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
) -> None:
    """Give `profiles` and `user_filters` the same shape on every SQLite database.

    Deltas 78/01 and 78/02 rebuilt these two tables to make `full_user_id` `NOT NULL`,
    but they only define `run_upgrade`, so the rebuild is skipped by the "initialise a
    new database" code path. The newest full schema snapshot (72) predates them, so
    every SQLite server installed since has been left with the old shape: `full_user_id`
    nullable and not unique, and `user_id` `NOT NULL` instead.

    Delta 78/02 also lost the `user_filters_unique` index. It created the index on its
    temporary table under a name the original table still held, so `IF NOT EXISTS` made
    it a no-op, and dropping the original table then took the only copy of the index
    with it. Delta 78/03 was written to repair precisely that, but it defines
    `run_update`, which Synapse never calls, so it has never run on any database.

    The rebuild is unconditional because SQLite stores `CREATE TABLE` statements
    verbatim: rebuilding only where the shape looks wrong would leave the two code paths
    with the same columns but different stored text, which still counts as a difference.

    Postgres needs none of this. Its half of delta 78 only validates a constraint that
    an earlier `.sql` delta added, and `.sql` deltas run on both code paths, so both
    already agree.
    """
    if isinstance(database_engine, PostgresEngine):
        return

    # By this point `full_user_id` is populated: on an upgrading database delta 78 filled
    # it in, and a database that skipped delta 78 has only ever been written to by code
    # that sets it. Check anyway rather than risk failing an upgrade on the `NOT NULL`,
    # which would stop the server from starting.
    for table in ("profiles", "user_filters"):
        if _has_null_full_user_ids(cur, table):
            logger.warning(
                "Not reshaping `%s`: it has rows with no `full_user_id`, which the "
                "rebuilt table would not allow. This is not expected; please report it.",
                table,
            )
            return

    logger.info("Rebuilding profiles and user_filters to make full_user_id NOT NULL")

    cur.execute("DROP TABLE IF EXISTS temp_profiles")
    cur.execute(_CREATE_TEMP_PROFILES)
    cur.execute(
        """
        INSERT INTO temp_profiles (full_user_id, user_id, displayname, avatar_url, fields)
            SELECT full_user_id, user_id, displayname, avatar_url, fields FROM profiles
        """
    )
    cur.execute("DROP TABLE profiles")
    cur.execute("ALTER TABLE temp_profiles RENAME TO profiles")

    cur.execute("DROP TABLE IF EXISTS temp_user_filters")
    cur.execute(_CREATE_TEMP_USER_FILTERS)
    cur.execute(
        """
        INSERT INTO temp_user_filters (full_user_id, user_id, filter_id, filter_json)
            SELECT full_user_id, user_id, filter_id, filter_json FROM user_filters
        """
    )
    cur.execute("DROP TABLE user_filters")
    cur.execute("ALTER TABLE temp_user_filters RENAME TO user_filters")

    # Recreate the indexes the rebuilds dropped. These have to come after the renames,
    # or they would be created against the temporary tables and then thrown away with
    # the originals — which is the mistake delta 78/02 made.
    #
    # `profiles_full_user_id_key` and `full_users_unique_idx` are otherwise created by
    # background updates registered in delta 76, which use `IF NOT EXISTS` and so become
    # no-ops if they have not run yet.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS profiles_full_user_id_key ON profiles (full_user_id)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS user_filters_unique ON user_filters (user_id, filter_id)"
    )
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS full_users_unique_idx ON user_filters (full_user_id, filter_id)"
    )
