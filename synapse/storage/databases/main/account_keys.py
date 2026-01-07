#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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

from typing import TYPE_CHECKING, Collection, cast

from signedjson.key import (
    decode_signing_key_base64,
    encode_signing_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.types import SigningKey

from synapse.api.errors import SynapseError
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.types import UserID, get_domain_from_id

if TYPE_CHECKING:
    from synapse.server import HomeServer


class AccountKeysStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

    async def get_or_create_local_account_key_user_id(
        self, account_name_user_id: str
    ) -> tuple[str, SigningKey]:
        """
        Get or create an account key for the given account name user ID.
        The user ID must belong to this server.

        Args:
            account_name_user_id: An account name user ID e.g "@alice:example.com"
        Returns:
            A tuple of account key user ID e.g @l8Hft5qXKn1vfHrg3p4+W8gELQVo8N13JkluMfmn2sQ:example.com
            and the private key for the account.
        Raises:
            if the provided account name user ID is not owned by this homeserver, or if the user
            ID is invalid in some way.
        """
        if not self.hs.is_mine_id(account_name_user_id):
            raise SynapseError(
                500,
                (
                    "get_or_create_local_account_key_user_id: this server cannot"
                    f" create an account key for other servers: {account_name_user_id}"
                ),
            )

        row = await self.db_pool.simple_select_one(
            table="account_keys",
            keyvalues={
                "account_name_user_id": account_name_user_id,
            },
            retcols=["account_key_user_id", "signing_key"],
            allow_none=True,
            desc="get_or_create_local_account_key_user_id.get_key_txn",
        )
        if row is not None:
            return row[0], decode_account_key(row[1])

        # create a new account key for this account inside a txn to ensure we lock correctly.
        def create_key_txn(txn: LoggingTransaction) -> tuple[str, str]:
            signing_key = generate_signing_key("1")
            account_key_user_id = UserID.from_verify_key(
                get_domain_from_id(account_name_user_id),
                get_verify_key(signing_key),
            )

            # Race to insert the key. The first one to make it will be returned here as we don't clobber
            sql = (
                "INSERT INTO account_keys(account_name_user_id, account_key_user_id, account_domain, signing_key)"
                " VALUES(?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING"
            )
            txn.execute(
                sql,
                (
                    account_name_user_id,
                    account_key_user_id.to_string(),
                    account_key_user_id.domain,
                    encode_signing_key_base64(signing_key),
                ),
            )
            sql = "SELECT account_key_user_id, signing_key FROM account_keys WHERE account_name_user_id = ?"
            txn.execute(sql, (account_name_user_id,))
            return cast(tuple[str, str], txn.fetchone())

        row = await self.db_pool.runInteraction(
            "get_or_create_local_account_key_user_id.create_key_txn",
            create_key_txn,
        )
        return row[0], decode_account_key(row[1])

    async def get_account_name_user_ids_for_account_key_user_ids(
        self,
        account_key_user_ids: Collection[str],
    ) -> dict[str, str]:
        """
        Fetch the verified account name user IDs for the given account key user IDs. Unknown account key
        user IDs will be omitted from the dict.

        Args:
            account_key_user_ids: A list of user IDs in account key format e.g
            ["@l8Hft5qXKn1vfHrg3p4-W8gELQVo8N13JkluMfmn2sQ:example.com"]

        Returns:
            A map of account key user IDs to account name user IDs e.g.
            {"@l8Hft5qXKn1vfHrg3p4-W8gELQVo8N13JkluMfmn2sQ:example.com":"@alice:example.com"}
        """

        clause, args = make_in_list_sql_clause(
            self.database_engine, "account_key_user_id", account_key_user_ids
        )

        def f(txn: LoggingTransaction) -> list[tuple[str, str]]:
            sql = f"SELECT account_key_user_id, account_name_user_id FROM account_keys WHERE {clause} AND account_name_user_id IS NOT NULL"
            txn.execute(sql, args)
            return cast(list[tuple[str, str]], txn.fetchall())

        rows = await self.db_pool.runInteraction(
            "get_account_name_user_ids_for_account_key_user_ids", f
        )
        return {row[0]: row[1] for row in rows}

    async def store_verified_account_name_user_ids(
        self, key_to_name: dict[str, str], timestamp: int
    ) -> None:
        """
        Store the verified account names for the given account key user IDs.

        Args:
            key_to_name: A map from account key user ID to verified account name user ID.
            timestamp: The current time, used for marking when this account was verified.
        """
        await self.db_pool.simple_upsert_many(
            table="account_keys",
            key_names=["account_key_user_id"],
            key_values=[(k,) for k in key_to_name.keys()],
            value_names=["account_name_user_id", "account_domain", "verified_at_ms"],
            value_values=[
                (n, get_domain_from_id(n), timestamp) for n in key_to_name.values()
            ],
            desc="store_verified_account_name_user_ids",
        )

    async def store_unverified_account_key_user_ids(
        self,
        user_ids: list[str],
    ) -> None:
        """
        Store unverified account key user IDs. Does nothing if the account key user ID is already
        verified.

        Args:
            user_ids: A list of account key user IDs.
        """
        await self.db_pool.simple_upsert_many(
            table="account_keys",
            key_names=["account_key_user_id"],
            key_values=[(k,) for k in user_ids],
            desc="store_unverified_account_key_user_ids",
            value_names=["account_domain"],
            value_values=[(get_domain_from_id(k),) for k in user_ids],
        )

    async def get_unverified_account_key_user_ids(
        self,
        domain: str,
    ) -> list[str]:
        """
        Get a list of unverified user IDs for the given domain.

        Args:
            domain: The domain to query
        Returns:
            A list of unverified account key user IDs.
        """
        # simple_select_onecol does not support IS NULL
        result = await self.db_pool.execute(
            "get_unverified_account_key_user_ids",
            "SELECT account_key_user_id FROM account_keys WHERE account_domain = ? AND account_name_user_id IS NULL",
            domain,
        )
        return [r[0] for r in result]


def decode_account_key(signing_key: str) -> SigningKey:
    return decode_signing_key_base64(
        "ed25519",
        "1",
        signing_key,
    )
