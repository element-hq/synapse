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

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, Sqlite3Engine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if isinstance(database_engine, Sqlite3Engine):
        idx_sql = """
        CREATE UNIQUE INDEX IF NOT EXISTS user_filters_full_user_id_unique ON
        user_filters (full_user_id, filter_id)
        """
        cur.execute(idx_sql)
