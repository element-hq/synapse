#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 Beeper
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
Adds a postgres SEQUENCE for generating application service transaction IDs.
"""

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if isinstance(database_engine, PostgresEngine):
        # If we already have some AS TXNs we want to start from the current
        # maximum value. There are two potential places this is stored - the
        # actual TXNs themselves *and* the AS state table. At time of migration
        # it is possible the TXNs table is empty so we must include the AS state
        # last_txn as a potential option, and pick the maximum.

        cur.execute("SELECT COALESCE(max(txn_id), 0) FROM application_services_txns")
        row = cur.fetchone()
        assert row is not None
        txn_max = row[0]

        cur.execute("SELECT COALESCE(max(last_txn), 0) FROM application_services_state")
        row = cur.fetchone()
        assert row is not None
        last_txn_max = row[0]

        start_val = max(last_txn_max, txn_max) + 1

        cur.execute(
            "CREATE SEQUENCE application_services_txn_id_seq START WITH %s",
            (start_val,),
        )
