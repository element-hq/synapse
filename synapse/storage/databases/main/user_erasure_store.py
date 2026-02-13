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

from typing import Iterable, Mapping, cast

from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main import CacheInvalidationWorkerStore
from synapse.util.caches.descriptors import cached, cachedList


class UserErasureWorkerStore(CacheInvalidationWorkerStore):
    @cached()
    async def is_user_erased(self, user_id: str) -> bool:
        """
        Check if the given user id has requested erasure

        Args:
            user_id: full user id to check

        Returns:
            True if the user has requested erasure
        """
        result = await self.db_pool.simple_select_onecol(
            table="erased_users",
            keyvalues={"user_id": user_id},
            retcol="1",
            desc="is_user_erased",
        )
        return bool(result)

    @cachedList(cached_method_name="is_user_erased", list_name="user_ids")
    async def are_users_erased(self, user_ids: Iterable[str]) -> Mapping[str, bool]:
        """
        Checks which users in a list have requested erasure

        Args:
            user_ids: full user ids to check

        Returns:
            for each user, whether the user has requested erasure.
        """
        rows = cast(
            list[tuple[str]],
            await self.db_pool.simple_select_many_batch(
                table="erased_users",
                column="user_id",
                iterable=user_ids,
                retcols=("user_id",),
                desc="are_users_erased",
            ),
        )
        erased_users = {row[0] for row in rows}

        return {u: u in erased_users for u in user_ids}

    async def mark_user_erased(self, user_id: str) -> None:
        """Indicate that user_id wishes their message history to be erased.

        Args:
            user_id: full user_id to be erased
        """

        def f(txn: LoggingTransaction) -> None:
            # first check if they are already in the list
            txn.execute("SELECT 1 FROM erased_users WHERE user_id = ?", (user_id,))
            if txn.fetchone():
                return

            # they are not already there: do the insert.
            txn.execute("INSERT INTO erased_users (user_id) VALUES (?)", (user_id,))

            self._invalidate_cache_and_stream(txn, self.is_user_erased, (user_id,))

        await self.db_pool.runInteraction("mark_user_erased", f)

    async def mark_user_not_erased(self, user_id: str) -> None:
        """Indicate that user_id is no longer erased.

        Args:
            user_id: full user_id to be un-erased
        """

        def f(txn: LoggingTransaction) -> None:
            # first check if they are already in the list
            txn.execute("SELECT 1 FROM erased_users WHERE user_id = ?", (user_id,))
            if not txn.fetchone():
                return

            # They are there, delete them.
            self.db_pool.simple_delete_one_txn(
                txn, "erased_users", keyvalues={"user_id": user_id}
            )

            self._invalidate_cache_and_stream(txn, self.is_user_erased, (user_id,))

        await self.db_pool.runInteraction("mark_user_not_erased", f)


class UserErasureStore(UserErasureWorkerStore):
    pass
