#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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

""" This module contains all the persistence actions done by the federation
package.

These actions are mostly only used by the :py:mod:`.replication` module.
"""

import logging
from typing import Optional, Tuple

from synapse.federation.units import Transaction
from synapse.storage.databases.main import DataStore
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


class TransactionActions:
    """Defines persistence actions that relate to handling Transactions."""

    def __init__(self, datastore: DataStore):
        self.store = datastore

    async def have_responded(
        self, origin: str, transaction: Transaction
    ) -> Optional[Tuple[int, JsonDict]]:
        """Have we already responded to a transaction with the same id and
        origin?

        Returns:
            `None` if we have not previously responded to this transaction or a
            2-tuple of `(int, dict)` representing the response code and response body.
        """
        transaction_id = transaction.transaction_id
        if not transaction_id:
            raise RuntimeError("Cannot persist a transaction with no transaction_id")

        return await self.store.get_received_txn_response(transaction_id, origin)

    async def set_response(
        self, origin: str, transaction: Transaction, code: int, response: JsonDict
    ) -> None:
        """Persist how we responded to a transaction."""
        transaction_id = transaction.transaction_id
        if not transaction_id:
            raise RuntimeError("Cannot persist a transaction with no transaction_id")

        await self.store.set_received_txn_response(
            transaction_id, origin, code, response
        )
