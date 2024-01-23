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
Adds a postgres SEQUENCE for generating guest user IDs.
"""

from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.registration import (
    find_max_generated_user_id_localpart,
)
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if not isinstance(database_engine, PostgresEngine):
        return

    next_id = find_max_generated_user_id_localpart(cur) + 1
    cur.execute("CREATE SEQUENCE user_id_seq START WITH %s", (next_id,))
