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

import time

from synapse.config.homeserver import HomeServerConfig
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine

ALTER_TABLE = "ALTER TABLE remote_media_cache ADD COLUMN last_access_ts BIGINT"


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    cur.execute(ALTER_TABLE)


def run_upgrade(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
    config: HomeServerConfig,
) -> None:
    cur.execute(
        "UPDATE remote_media_cache SET last_access_ts = ?",
        (int(time.time() * 1000),),
    )
