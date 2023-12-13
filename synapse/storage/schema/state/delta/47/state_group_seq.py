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
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if isinstance(database_engine, PostgresEngine):
        # if we already have some state groups, we want to start making new
        # ones with a higher id.
        cur.execute("SELECT max(id) FROM state_groups")
        row = cur.fetchone()
        assert row is not None

        if row[0] is None:
            start_val = 1
        else:
            start_val = row[0] + 1

        cur.execute("CREATE SEQUENCE state_group_id_seq START WITH %s", (start_val,))
