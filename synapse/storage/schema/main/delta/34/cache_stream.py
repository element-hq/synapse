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

import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine
from synapse.storage.prepare_database import get_statements

logger = logging.getLogger(__name__)


# This stream is used to notify workers over replication that some caches have
# been invalidated that they cannot infer from the other streams.
CREATE_TABLE = """
CREATE TABLE cache_invalidation_stream (
    stream_id       BIGINT,
    cache_func      TEXT,
    keys            TEXT[],
    invalidation_ts BIGINT
);

CREATE INDEX cache_invalidation_stream_id ON cache_invalidation_stream(stream_id);
"""


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if not isinstance(database_engine, PostgresEngine):
        return

    for statement in get_statements(CREATE_TABLE.splitlines()):
        cur.execute(statement)
