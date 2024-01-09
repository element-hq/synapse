#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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


from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine
from synapse.storage.prepare_database import get_statements

FIX_INDEXES = """
-- rebuild indexes as uniques
DROP INDEX groups_invites_g_idx;
CREATE UNIQUE INDEX group_invites_g_idx ON group_invites(group_id, user_id);
DROP INDEX groups_users_g_idx;
CREATE UNIQUE INDEX group_users_g_idx ON group_users(group_id, user_id);

-- rename other indexes to actually match their table names..
DROP INDEX groups_users_u_idx;
CREATE INDEX group_users_u_idx ON group_users(user_id);
DROP INDEX groups_invites_u_idx;
CREATE INDEX group_invites_u_idx ON group_invites(user_id);
DROP INDEX groups_rooms_g_idx;
CREATE UNIQUE INDEX group_rooms_g_idx ON group_rooms(group_id, room_id);
DROP INDEX groups_rooms_r_idx;
CREATE INDEX group_rooms_r_idx ON group_rooms(room_id);
"""


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    rowid = database_engine.row_id_name

    # remove duplicates from group_users & group_invites tables
    cur.execute(
        """
        DELETE FROM group_users WHERE %s NOT IN (
           SELECT min(%s) FROM group_users GROUP BY group_id, user_id
        );
    """
        % (rowid, rowid)
    )
    cur.execute(
        """
        DELETE FROM group_invites WHERE %s NOT IN (
           SELECT min(%s) FROM group_invites GROUP BY group_id, user_id
        );
    """
        % (rowid, rowid)
    )

    for statement in get_statements(FIX_INDEXES.splitlines()):
        cur.execute(statement)
