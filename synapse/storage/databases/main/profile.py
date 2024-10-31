#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import json
from typing import TYPE_CHECKING, Dict, Optional, Tuple, cast

from canonicaljson import encode_canonical_json

from synapse.api.constants import ProfileFields
from synapse.api.errors import StoreError
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.roommember import ProfileInfo
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict, JsonValue, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


# The number of bytes that the serialized profile can have.
MAX_PROFILE_SIZE = 65536


class ProfileWorkerStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)
        self.server_name: str = hs.hostname
        self.database_engine = database.engine
        self.db_pool.updates.register_background_index_update(
            "profiles_full_user_id_key_idx",
            index_name="profiles_full_user_id_key",
            table="profiles",
            columns=["full_user_id"],
            unique=True,
        )

        self.db_pool.updates.register_background_update_handler(
            "populate_full_user_id_profiles", self.populate_full_user_id_profiles
        )

    async def populate_full_user_id_profiles(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Background update to populate the column `full_user_id` of the table
        profiles from entries in the column `user_local_part` of the same table
        """

        lower_bound_id = progress.get("lower_bound_id", "")

        def _get_last_id(txn: LoggingTransaction) -> Optional[str]:
            sql = """
                    SELECT user_id FROM profiles
                    WHERE user_id > ?
                    ORDER BY user_id
                    LIMIT 1 OFFSET 1000
                  """
            txn.execute(sql, (lower_bound_id,))
            res = txn.fetchone()
            if res:
                upper_bound_id = res[0]
                return upper_bound_id
            else:
                return None

        def _process_batch(
            txn: LoggingTransaction, lower_bound_id: str, upper_bound_id: str
        ) -> None:
            sql = """
                    UPDATE profiles
                    SET full_user_id = '@' || user_id || ?
                    WHERE ? < user_id AND user_id <= ? AND full_user_id IS NULL
                   """
            txn.execute(sql, (f":{self.server_name}", lower_bound_id, upper_bound_id))

        def _final_batch(txn: LoggingTransaction, lower_bound_id: str) -> None:
            sql = """
                    UPDATE profiles
                    SET full_user_id = '@' || user_id || ?
                    WHERE ? < user_id AND full_user_id IS NULL
                   """
            txn.execute(
                sql,
                (
                    f":{self.server_name}",
                    lower_bound_id,
                ),
            )

            if isinstance(self.database_engine, PostgresEngine):
                sql = """
                        ALTER TABLE profiles VALIDATE CONSTRAINT full_user_id_not_null
                      """
                txn.execute(sql)

        upper_bound_id = await self.db_pool.runInteraction(
            "populate_full_user_id_profiles", _get_last_id
        )

        if upper_bound_id is None:
            await self.db_pool.runInteraction(
                "populate_full_user_id_profiles", _final_batch, lower_bound_id
            )

            await self.db_pool.updates._end_background_update(
                "populate_full_user_id_profiles"
            )
            return 1

        await self.db_pool.runInteraction(
            "populate_full_user_id_profiles",
            _process_batch,
            lower_bound_id,
            upper_bound_id,
        )

        progress["lower_bound_id"] = upper_bound_id

        await self.db_pool.runInteraction(
            "populate_full_user_id_profiles",
            self.db_pool.updates._background_update_progress_txn,
            "populate_full_user_id_profiles",
            progress,
        )

        return 50

    async def get_profileinfo(self, user_id: UserID) -> ProfileInfo:
        """
        Fetch the display name and avatar URL of a user.

        Args:
            user_id: The user ID to fetch the profile for.

        Returns:
            The user's display name and avatar URL. Values may be null if unset
             or if the user doesn't exist.
        """
        profile = await self.db_pool.simple_select_one(
            table="profiles",
            keyvalues={"full_user_id": user_id.to_string()},
            retcols=("displayname", "avatar_url"),
            desc="get_profileinfo",
            allow_none=True,
        )
        if profile is None:
            # no match
            return ProfileInfo(None, None)

        return ProfileInfo(avatar_url=profile[1], display_name=profile[0])

    async def get_profile_displayname(self, user_id: UserID) -> Optional[str]:
        """
        Fetch the display name of a user.

        Args:
            user_id: The user to get the display name for.

        Raises:
            404 if the user does not exist.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="profiles",
            keyvalues={"full_user_id": user_id.to_string()},
            retcol="displayname",
            desc="get_profile_displayname",
        )

    async def get_profile_avatar_url(self, user_id: UserID) -> Optional[str]:
        """
        Fetch the avatar URL of a user.

        Args:
            user_id: The user to get the avatar URL for.

        Raises:
            404 if the user does not exist.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="profiles",
            keyvalues={"full_user_id": user_id.to_string()},
            retcol="avatar_url",
            desc="get_profile_avatar_url",
        )

    async def get_profile_field(
        self, user_id: UserID, field_name: str
    ) -> Optional[str]:
        """
        Get a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The custom profile field name.

        Returns:
            The string value if the field exists, otherwise raises 404.
        """
        result = await self.get_profile_fields(user_id)
        if field_name not in result:
            raise StoreError(404, "No row found")
        return result[field_name]

    async def get_profile_fields(self, user_id: UserID) -> Dict[str, str]:
        """
        Get all custom profile fields for a user.

        Args:
            user_id: The user's ID.

        Returns:
            A dictionary of custom profile fields.
        """
        result = await self.db_pool.simple_select_one_onecol(
            table="profiles",
            keyvalues={"full_user_id": user_id.to_string()},
            retcol="fields",
            desc="get_profile_fields",
        )
        return result or {}

    async def create_profile(self, user_id: UserID) -> None:
        """
        Create a blank profile for a user.

        Args:
            user_id: The user to create the profile for.
        """
        user_localpart = user_id.localpart
        await self.db_pool.simple_insert(
            table="profiles",
            values={"user_id": user_localpart, "full_user_id": user_id.to_string()},
            desc="create_profile",
        )

    def _check_profile_size(
        self,
        txn: LoggingTransaction,
        user_id: UserID,
        new_field_name: str,
        new_value: JsonValue,
    ) -> None:
        # For each entry there are 4 quotes (2 each for key and value), 1 colon,
        # and 1 comma.
        PER_VALUE_EXTRA = 6

        # Add the size of the current custom profile fields, ignoring the entry
        # which will be overwritten.
        if isinstance(txn.database_engine, PostgresEngine):
            size_sql = """
            SELECT
                OCTET_LENGTH((fields - ?)::text), OCTET_LENGTH(displayname), OCTET_LENGTH(avatar_url)
            FROM profiles
            WHERE
                user_id = ?
            """
        else:
            size_sql = """
            SELECT SUM(LENGTH(name)) + SUM(LENGTH(value)), COUNT(name) AS total_bytes
            FROM profiles
            WHERE
                user_id = ?
            """
        txn.execute(
            size_sql,
            (
                new_field_name,
                user_id.localpart,
            ),
        )
        row = cast(Tuple[Optional[int], Optional[int], Optional[int]], txn.fetchone())

        # The values return null if the column is null.
        total_bytes = (
            # Discount the opening and closing braces to avoid double counting,
            # but add one for a comma.
            (row[0] - 1 if row[0] else 0)
            + (
                row[1] + len("displayname") + PER_VALUE_EXTRA
                if new_field_name != ProfileFields.DISPLAYNAME and row[1]
                else 0
            )
            + (
                row[2] + len("avatar_url") + PER_VALUE_EXTRA
                if new_field_name != ProfileFields.AVATAR_URL and row[2]
                else 0
            )
        )

        # Add the length of the field being added + the braces.
        total_bytes += len(encode_canonical_json({new_field_name: new_value}))

        if total_bytes > MAX_PROFILE_SIZE:
            raise StoreError(400, "Profile too large")

    async def set_profile_displayname(
        self, user_id: UserID, new_displayname: Optional[str]
    ) -> None:
        """
        Set the display name of a user.

        Args:
            user_id: The user's ID.
            new_displayname: The new display name. If this is None, the user's display
                name is removed.
        """
        user_localpart = user_id.localpart

        def set_profile_displayname(txn: LoggingTransaction) -> None:
            if new_displayname is not None:
                self._check_profile_size(
                    txn, user_id, ProfileFields.DISPLAYNAME, new_displayname
                )

            self.db_pool.simple_upsert_txn(
                txn,
                table="profiles",
                keyvalues={"user_id": user_localpart},
                values={
                    "displayname": new_displayname,
                    "full_user_id": user_id.to_string(),
                },
            )

        await self.db_pool.runInteraction(
            "set_profile_displayname", set_profile_displayname
        )

    async def set_profile_avatar_url(
        self, user_id: UserID, new_avatar_url: Optional[str]
    ) -> None:
        """
        Set the avatar of a user.

        Args:
            user_id: The user's ID.
            new_avatar_url: The new avatar URL. If this is None, the user's avatar is
                removed.
        """
        user_localpart = user_id.localpart

        def set_profile_avatar_url(txn: LoggingTransaction) -> None:
            if new_avatar_url is not None:
                self._check_profile_size(
                    txn, user_id, ProfileFields.AVATAR_URL, new_avatar_url
                )

            self.db_pool.simple_upsert_txn(
                txn,
                table="profiles",
                keyvalues={"user_id": user_localpart},
                values={
                    "avatar_url": new_avatar_url,
                    "full_user_id": user_id.to_string(),
                },
            )

        await self.db_pool.runInteraction(
            "set_profile_avatar_url", set_profile_avatar_url
        )

    async def set_profile_field(
        self, user_id: UserID, field_name: str, new_value: JsonValue
    ) -> None:
        """
        Set a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The name of the custom profile field.
            new_value: The value of the custom profile field.
        """

        def set_profile_field(txn: LoggingTransaction) -> None:
            self._check_profile_size(txn, user_id, field_name, new_value)

            if isinstance(self.database_engine, PostgresEngine):
                from psycopg2.extras import Json

                sql = """
                INSERT INTO profiles (user_id, full_user_id, fields) VALUES (?, ?, json_build_object(?, ?::jsonb))
                ON CONFLICT (user_id)
                DO UPDATE SET full_user_id = EXCLUDED.full_user_id, fields = COALESCE(profiles.fields, '{}'::jsonb) || EXCLUDED.fields
                """

                txn.execute(
                    sql,
                    (
                        user_id.localpart,
                        user_id.to_string(),
                        field_name,
                        # encode to canonical JSON, but then pass as a JSON object
                        # since we have passing bytes disabled at the database driver
                        # level.
                        Json(json.loads(encode_canonical_json(new_value))),
                    ),
                )
            else:
                raise RuntimeError("Unsupported")

        await self.db_pool.runInteraction("set_profile_field", set_profile_field)

    async def delete_profile_field(self, user_id: UserID, field_name: str) -> None:
        """
        Set a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The name of the custom profile field.
        """

        def delete_profile_field(txn: LoggingTransaction) -> None:
            if isinstance(self.database_engine, PostgresEngine):
                sql = """
                UPDATE profiles SET fields = fields - ?
                WHERE user_id = ?
                """
            else:
                raise RuntimeError("Unsupported")
            txn.execute(
                sql,
                (field_name, user_id.localpart),
            )

        await self.db_pool.runInteraction("delete_profile_field", delete_profile_field)


class ProfileStore(ProfileWorkerStore):
    pass
