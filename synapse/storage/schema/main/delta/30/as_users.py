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
from typing import Iterable, cast

from synapse.config.appservice import load_appservices
from synapse.config.homeserver import HomeServerConfig
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine

logger = logging.getLogger(__name__)


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    # NULL indicates user was not registered by an appservice.
    try:
        cur.execute("ALTER TABLE users ADD COLUMN appservice_id TEXT")
    except Exception:
        # Maybe we already added the column? Hope so...
        pass


def run_upgrade(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
    config: HomeServerConfig,
) -> None:
    cur.execute("SELECT name FROM users")
    rows = cast(Iterable[tuple[str]], cur.fetchall())

    config_files = []
    try:
        config_files = config.appservice.app_service_config_files
    except AttributeError:
        logger.warning("Could not get app_service_config_files from config")

    appservices = load_appservices(config.server.server_name, config_files)

    owned: dict[str, list[str]] = {}

    for row in rows:
        user_id = row[0]
        for appservice in appservices:
            if appservice.is_exclusive_user(user_id):
                if user_id in owned.keys():
                    logger.error(
                        "user_id %s was owned by more than one application"
                        " service (IDs %s and %s); assigning arbitrarily to %s",
                        user_id,
                        owned[user_id],
                        appservice.id,
                        owned[user_id],
                    )
                owned.setdefault(appservice.id, []).append(user_id)

    for as_id, user_ids in owned.items():
        n = 100
        user_chunks = (user_ids[i : i + 100] for i in range(0, len(user_ids), n))
        for chunk in user_chunks:
            cur.execute(
                "UPDATE users SET appservice_id = ? WHERE name IN (%s)"
                % (",".join("?" for _ in chunk),),
                [as_id] + chunk,
            )
