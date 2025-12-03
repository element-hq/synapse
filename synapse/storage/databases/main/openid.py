#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
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


from synapse.storage._base import SQLBaseStore
from synapse.storage.database import LoggingTransaction


class OpenIdStore(SQLBaseStore):
    async def insert_open_id_token(
        self, token: str, ts_valid_until_ms: int, user_id: str
    ) -> None:
        await self.db_pool.simple_insert(
            table="open_id_tokens",
            values={
                "token": token,
                "ts_valid_until_ms": ts_valid_until_ms,
                "user_id": user_id,
            },
            desc="insert_open_id_token",
        )

    async def get_user_id_for_open_id_token(
        self, token: str, ts_now_ms: int
    ) -> str | None:
        def get_user_id_for_token_txn(txn: LoggingTransaction) -> str | None:
            sql = (
                "SELECT user_id FROM open_id_tokens"
                " WHERE token = ? AND ? <= ts_valid_until_ms"
            )

            txn.execute(sql, (token, ts_now_ms))

            rows = txn.fetchall()
            if not rows:
                return None
            else:
                return rows[0][0]

        return await self.db_pool.runInteraction(
            "get_user_id_for_token", get_user_id_for_token_txn
        )
