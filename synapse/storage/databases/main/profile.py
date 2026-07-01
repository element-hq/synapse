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
import logging
from typing import TYPE_CHECKING, Collection, Iterable, cast

import attr
from canonicaljson import encode_canonical_json

from synapse.api.constants import ProfileFields, ProfileUpdateAction
from synapse.api.errors import Codes, StoreError
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.replication.tcp.streams._base import ProfileUpdatesStream
from synapse.storage._base import SQLBaseStore, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.roommember import ProfileInfo
from synapse.storage.engines import PostgresEngine, Sqlite3Engine
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import JsonDict, JsonValue, UserID
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# The number of bytes that the serialized profile can have.
MAX_PROFILE_SIZE = 65536

# Prunes entries out of the `profile_updates` and `profile_updates_per_user` tables
# that are more than this old.
PRUNE_PROFILE_UPDATES_AGE = Duration(days=30)

# The number of rows to delete at once when pruning old entries out of the
# `profile_updates` and `profile_updates_per_user` tables.
PRUNE_PROFILE_UPDATES_BATCH_SIZE = 1000


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ProfileUpdate:
    """An update to a user's profile."""

    stream_id: int
    user_id: str
    action: str
    field_name: str | None


class ProfileWorkerStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)
        self.server_name: str = hs.hostname
        self._instance_name: str = hs.get_instance_name()
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

        self._can_write_to_profile_updates = (
            self._instance_name in hs.config.worker.writers.profile_updates
        )
        self._profile_updates_id_gen: MultiWriterIdGenerator = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="profile_updates",
            server_name=self.server_name,
            instance_name=self._instance_name,
            tables=[
                ("profile_updates", "instance_name", "stream_id"),
            ],
            sequence_name="profile_updates_sequence",
            writers=hs.config.worker.writers.profile_updates,
        )
        if hs.config.worker.run_background_tasks:
            self.clock.looping_call(
                self._prune_profile_updates,
                Duration(hours=1),
            )

    async def populate_full_user_id_profiles(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Background update to populate the column `full_user_id` of the table
        profiles from entries in the column `user_local_part` of the same table
        """

        lower_bound_id = progress.get("lower_bound_id", "")

        def _get_last_id(txn: LoggingTransaction) -> str | None:
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

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == ProfileUpdatesStream.NAME:
            self._profile_updates_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

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

    async def get_profile_displayname(self, user_id: UserID) -> str | None:
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

    async def get_profile_avatar_url(self, user_id: UserID) -> str | None:
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
    ) -> JsonValue | dict[str, JsonValue]:
        """
        Get a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The custom profile field name.

        Returns:
            The string value if the field exists, otherwise raises 404.
        """

        def get_profile_field(
            txn: LoggingTransaction,
        ) -> JsonValue | dict[str, JsonValue]:
            # This will error if field_name has double quotes in it, but that's not
            # possible due to the grammar.
            field_path = f'$."{field_name}"'

            if isinstance(self.database_engine, PostgresEngine):
                sql = """
                SELECT JSONB_PATH_EXISTS(fields, ?), JSONB_EXTRACT_PATH(fields, ?)
                FROM profiles
                WHERE user_id = ?
                """
                txn.execute(
                    sql,
                    (field_path, field_name, user_id.localpart),
                )

                # Test exists first since value being None is used for both
                # missing and a null JSON value.
                exists, value = cast(
                    tuple[bool, JsonValue | dict[str, JsonValue]], txn.fetchone()
                )
                if not exists:
                    raise StoreError(404, "No row found")
                return value

            else:
                sql = """
                SELECT JSON_TYPE(fields, ?), JSON_EXTRACT(fields, ?)
                FROM profiles
                WHERE user_id = ?
                """
                txn.execute(
                    sql,
                    (field_path, field_path, user_id.localpart),
                )

                # If value_type is None, then the value did not exist.
                value_type, value = cast(
                    tuple[str | None, JsonValue | dict[str, JsonValue]], txn.fetchone()
                )
                if not value_type:
                    raise StoreError(404, "No row found")
                # If value_type is object or array, then need to deserialize the JSON.
                # Scalar values are properly returned directly.
                if value_type in ("object", "array"):
                    assert isinstance(value, str)
                    return json.loads(value)
                return value

        return await self.db_pool.runInteraction("get_profile_field", get_profile_field)

    async def get_profile_fields(self, user_id: UserID) -> dict[str, str]:
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
        # The SQLite driver doesn't automatically convert JSON to
        # Python objects
        if isinstance(self.database_engine, Sqlite3Engine) and result:
            result = json.loads(result)
        return result or {}

    def get_max_profile_updates_stream_id(self) -> int:
        """Get the current maximum stream_id for profile updates."""
        return self._profile_updates_id_gen.get_current_token()

    def get_profile_updates_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._profile_updates_id_gen

    async def get_updated_profile_updates(
        self, *, from_id: int, to_id: int, limit: int
    ) -> list[tuple[int, str, str, str | None]]:
        """Get updates to profile updates between two stream IDs.

        Bounds: from_id < ... <= to_id

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            list of tuples representing stream_id, user_id, action and field_name
        """
        if from_id >= to_id:
            return []

        def _get_updated_profile_updates_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[int, str, str, str | None]]:
            sql = """
            SELECT
                stream_id, user_id, action, field_name
            FROM profile_updates
            WHERE
                ? < stream_id AND stream_id <= ?
            ORDER BY stream_id ASC LIMIT ?
            """
            txn.execute(sql, (from_id, to_id, limit))
            return cast(list[tuple[int, str, str, str | None]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_updated_profile_updates", _get_updated_profile_updates_txn
        )

    async def get_profile_updates_for_fields(
        self,
        *,
        from_id: int,
        to_id: int,
        field_names: Iterable[str],
    ) -> list[ProfileUpdate]:
        """Get profile update markers for the given fields in a stream range.

        Bounds: from_id < ... <= to_id

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            field_names: List of field names to filter against.

        Returns:
            list of ProfileUpdates update rows
        """
        if from_id >= to_id:
            return []

        field_names = list(field_names)
        if not field_names:
            return []

        def _get_profile_updates_for_fields_txn(
            txn: LoggingTransaction,
        ) -> list[ProfileUpdate]:
            clause, args = make_in_list_sql_clause(
                txn.database_engine, "field_name", field_names
            )
            sql = (
                "SELECT stream_id, user_id, action, field_name"
                " FROM profile_updates"
                f" WHERE ? < stream_id AND stream_id <= ? AND ({clause}"
                " OR action != ?) "
                " ORDER BY stream_id ASC"
            )
            txn.execute(sql, (from_id, to_id, *args, ProfileUpdateAction.UPDATE.value))
            rows = cast(list[tuple[int, str, str, str | None]], txn.fetchall())

            updates: list[ProfileUpdate] = []
            for stream_id, user_id, action, field_name in rows:
                updates.append(
                    ProfileUpdate(
                        stream_id=stream_id,
                        user_id=user_id,
                        action=action,
                        field_name=field_name,
                    )
                )

            return updates

        return await self.db_pool.runInteraction(
            "get_profile_updates_for_fields", _get_profile_updates_for_fields_txn
        )

    async def get_profile_updates_for_user_and_fields(
        self,
        *,
        from_id: int,
        to_id: int,
        user_id: str,
        field_names: set[str],
        include_users: set[str] | None = None,
    ) -> list[ProfileUpdate]:
        """Get profile update markers for a user in a stream range.

        The returned profile update rows are restricted to those with a
        corresponding `profile_updates_per_user` row for the syncing user.

        Bounds: from_id < ... <= to_id

        Args:
            from_id: The starting stream ID (exclusive).
            to_id: The ending stream ID (inclusive).
            user_id: The full user ID to filter on.
            field_names: Set of field names to filter update actions against.
            include_users: If given, only include updates for these user IDs.

        Returns:
            A list of ProfileUpdates update rows.
        """
        if from_id >= to_id:
            return []

        if len(field_names) == 0:
            return []

        if include_users is not None and len(include_users) == 0:
            # All updates have been filtered out by lazy-loading.
            return []

        def _get_profile_updates_for_user_and_fields_txn(
            txn: LoggingTransaction,
        ) -> list[ProfileUpdate]:
            field_clause, field_args = make_in_list_sql_clause(
                txn.database_engine, "pu.field_name", field_names
            )
            user_clause = ""
            user_args: list[str] = []
            if include_users is not None:
                # Filter out rows that aren't in `include_users`, if defined.
                # This is only relevant when lazy-loading.
                user_clause, user_args = make_in_list_sql_clause(
                    txn.database_engine, "pu.user_id", include_users
                )
                user_clause = f"AND {user_clause}"

            # Retrieve profile updates where there's a corresponding row in
            # `profile_updates_per_user` within the given `stream_id` bounds
            # and the `user_id` and `field_names` match.
            sql = f"""
                SELECT pu.stream_id, pu.user_id, pu.action, pu.field_name
                  FROM profile_updates AS pu
                  INNER JOIN profile_updates_per_user AS puf
                  ON pu.stream_id = puf.stream_id
                  WHERE ? < pu.stream_id AND pu.stream_id <= ?
                  AND puf.user_id = ?
                  {user_clause}
                  AND ({field_clause} OR pu.action != ?)
                  ORDER BY pu.stream_id ASC
            """

            txn.execute(
                sql,
                (
                    from_id,
                    to_id,
                    user_id,
                    *user_args,
                    *field_args,
                    ProfileUpdateAction.UPDATE.value,
                ),
            )
            rows = cast(list[tuple[int, str, str, str | None]], txn.fetchall())

            updates: list[ProfileUpdate] = []
            for stream_id, updated_user_id, action, field_name in rows:
                updates.append(
                    ProfileUpdate(
                        stream_id=stream_id,
                        user_id=updated_user_id,
                        action=action,
                        field_name=field_name,
                    )
                )

            return updates

        return await self.db_pool.runInteraction(
            "get_profile_updates_for_user_and_fields",
            _get_profile_updates_for_user_and_fields_txn,
        )

    async def get_profile_data_for_users(
        self, user_ids: Collection[str]
    ) -> dict[str, dict[str, JsonValue | dict[str, JsonValue]]]:
        """Fetch displayname/avatar_url/custom fields for a list of users.

        Currently, this returns only local users as the `profiles` table only
        tracks local users.

        Args:
            user_ids: List of user IDs to filter against.

        Returns:
            Dictionary of displayname/avatar_url/custom fields for a list of users.
        """
        if not user_ids:
            return {}

        rows = await self.db_pool.simple_select_many_batch(
            table="profiles",
            column="full_user_id",
            iterable=user_ids,
            retcols=("full_user_id", "displayname", "avatar_url", "fields"),
            desc="get_profile_data_for_users",
        )

        results: dict[str, dict[str, JsonValue | dict[str, JsonValue]]] = {}
        for full_user_id, displayname, avatar_url, fields in rows:
            user_fields = fields or {}
            # The SQLite driver doesn't automatically convert JSON to
            # Python objects
            if isinstance(self.database_engine, Sqlite3Engine) and fields:
                user_fields = json.loads(fields)
            base_fields = {
                ProfileFields.DISPLAYNAME: displayname,
                ProfileFields.AVATAR_URL: avatar_url,
            }
            user_fields.update(base_fields)

            results[full_user_id] = user_fields

        return results

    async def add_profile_updates(
        self,
        user_id: UserID,
        action: ProfileUpdateAction,
        updated_fields: set[str] | None,
    ) -> int:
        """Persist profile update markers and return the last stream ID."""
        assert self._can_write_to_profile_updates

        if action == ProfileUpdateAction.UPDATE and not updated_fields:
            return self._profile_updates_id_gen.get_current_token()
        elif action == ProfileUpdateAction.LEFT_ROOM:
            assert not updated_fields

        user_id_str = user_id.to_string()

        def _add_profile_updates_txn(txn: LoggingTransaction) -> int:
            values = []
            inserted_ts = self.clock.time_msec()
            if updated_fields:
                stream_ids = self._profile_updates_id_gen.get_next_mult_txn(
                    txn, len(updated_fields)
                )
                for stream_id, field_name in zip(stream_ids, updated_fields):
                    values.append(
                        [
                            stream_id,
                            self._instance_name,
                            user_id_str,
                            action.value,
                            field_name,
                            inserted_ts,
                        ]
                    )
            else:
                stream_ids = [self._profile_updates_id_gen.get_next_txn(txn)]
                values.append(
                    [
                        stream_ids[0],
                        self._instance_name,
                        user_id_str,
                        action.value,
                        None,
                        inserted_ts,
                    ]
                )
            self.db_pool.simple_insert_many_txn(
                txn,
                table="profile_updates",
                keys=[
                    "stream_id",
                    "instance_name",
                    "user_id",
                    "action",
                    "field_name",
                    "inserted_ts",
                ],
                values=values,
            )

            return stream_ids[-1]

        return await self.db_pool.runInteraction(
            "add_profile_updates", _add_profile_updates_txn
        )

    async def track_profile_updates_per_user(
        self,
        stream_id: int,
        user_ids: set[str],
    ) -> None:
        """
        Create tracking rows for profile updater per target user interested in profile
        updates for the user triggering one, including themselves.

        Args:
            stream_id: Stream ID referencing a `profile_updates` stream ID.
            user_ids: A set of the full user IDs of the target users interested in
                this change.
        """

        def _track_profile_updates_per_user_txn(txn: LoggingTransaction) -> None:
            inserted_ts = self.clock.time_msec()
            values = [(stream_id, user_id, inserted_ts) for user_id in user_ids]
            self.db_pool.simple_insert_many_txn(
                txn,
                table="profile_updates_per_user",
                keys=[
                    "stream_id",
                    "user_id",
                    "inserted_ts",
                ],
                values=values,
            )

        return await self.db_pool.runInteraction(
            "track_profile_updates_per_user",
            _track_profile_updates_per_user_txn,
        )

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
        new_value: JsonValue | dict[str, JsonValue],
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
            txn.execute(
                size_sql,
                (new_field_name, user_id.localpart),
            )
        else:
            size_sql = """
            SELECT
                LENGTH(json_remove(fields, ?)), LENGTH(displayname), LENGTH(avatar_url)
            FROM profiles
            WHERE
                user_id = ?
            """
            txn.execute(
                size_sql,
                # This will error if field_name has double quotes in it, but that's not
                # possible due to the grammar.
                (f'$."{new_field_name}"', user_id.localpart),
            )
        row = cast(tuple[int | None, int | None, int | None], txn.fetchone())

        # The values return null if the column is null.
        total_bytes = (
            # Discount the opening and closing braces to avoid double counting,
            # but add one for a comma.
            # -2 + 1 = -1
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
            raise StoreError(400, "Profile too large", Codes.PROFILE_TOO_LARGE)

    async def set_profile_displayname(
        self, user_id: UserID, new_displayname: str | None
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
        self, user_id: UserID, new_avatar_url: str | None
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
        self,
        user_id: UserID,
        field_name: str,
        new_value: JsonValue | dict[str, JsonValue],
    ) -> None:
        """
        Set a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The name of the custom profile field.
            new_value: The value of the custom profile field.
        """

        # Encode to canonical JSON.
        canonical_value = encode_canonical_json(new_value)

        def set_profile_field(txn: LoggingTransaction) -> None:
            self._check_profile_size(txn, user_id, field_name, new_value)

            if isinstance(self.database_engine, PostgresEngine):
                from psycopg2.extras import Json

                # Note that the || jsonb operator is not recursive, any duplicate
                # keys will be taken from the second value.
                sql = """
                INSERT INTO profiles (user_id, full_user_id, fields) VALUES (?, ?, JSON_BUILD_OBJECT(?, ?::jsonb))
                ON CONFLICT (user_id)
                DO UPDATE SET full_user_id = EXCLUDED.full_user_id, fields = COALESCE(profiles.fields, '{}'::jsonb) || EXCLUDED.fields
                """

                txn.execute(
                    sql,
                    (
                        user_id.localpart,
                        user_id.to_string(),
                        field_name,
                        # Pass as a JSON object since we have passing bytes disabled
                        # at the database driver.
                        Json(json.loads(canonical_value)),
                    ),
                )
            else:
                # You may be tempted to use json_patch instead of providing the parameters
                # twice, but that recursively merges objects instead of replacing.
                sql = """
                INSERT INTO profiles (user_id, full_user_id, fields) VALUES (?, ?, JSON_OBJECT(?, JSON(?)))
                ON CONFLICT (user_id)
                DO UPDATE SET full_user_id = EXCLUDED.full_user_id, fields = JSON_SET(COALESCE(profiles.fields, '{}'), ?, JSON(?))
                """
                # This will error if field_name has double quotes in it, but that's not
                # possible due to the grammar.
                json_field_name = f'$."{field_name}"'

                txn.execute(
                    sql,
                    (
                        user_id.localpart,
                        user_id.to_string(),
                        json_field_name,
                        canonical_value,
                        json_field_name,
                        canonical_value,
                    ),
                )

        await self.db_pool.runInteraction("set_profile_field", set_profile_field)

    async def delete_profile_field(self, user_id: UserID, field_name: str) -> None:
        """
        Remove a custom profile field for a user.

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
                txn.execute(
                    sql,
                    (field_name, user_id.localpart),
                )
            else:
                sql = """
                UPDATE profiles SET fields = json_remove(fields, ?)
                WHERE user_id = ?
                """
                txn.execute(
                    sql,
                    # This will error if field_name has double quotes in it.
                    (f'$."{field_name}"', user_id.localpart),
                )

        await self.db_pool.runInteraction("delete_profile_field", delete_profile_field)

    async def delete_profile(self, user_id: UserID) -> None:
        """
        Deletes an entire user profile, including displayname, avatar_url and all custom fields.
        Used at user deactivation when erasure is requested.
        """

        await self.db_pool.simple_delete(
            desc="delete_profile",
            table="profiles",
            keyvalues={"full_user_id": user_id.to_string()},
        )

    async def clear_profile_updates_for_user(
        self, user_id: UserID, users_to_remove: set[str]
    ) -> None:
        """
        Clear all the ProfileUpdateAction.UPDATE rows from the
        `profile_updates_per_user` table from a particular user for
        a list of target users.

        This does not remove the stream ID row from `profile_updates` as it is
        likely other per user rows may refer to it. Our automatic pruning of old
        stream ID's will kick in later and clean up potential orphan `profile_updates`
        table rows.

        Args:
            user_id: The user's ID.
            users_to_remove: List of users to remove per user rows for.

        Returns:
            None
        """
        assert self._can_write_to_profile_updates
        if not users_to_remove:
            return

        def _clear_profile_updates_for_user_txn(
            txn: LoggingTransaction,
        ) -> None:
            # Delete profile updates where there's a corresponding row in
            # `profile_updates_per_user`.
            sql = """
                SELECT stream_id FROM profile_updates
                    WHERE user_id = ? AND action = ?
            """

            txn.execute(sql, (user_id.to_string(), ProfileUpdateAction.UPDATE.value))
            res = txn.fetchall()
            if not res:
                return

            stream_ids = [row[0] for row in res]

            user_clause, user_args = make_in_list_sql_clause(
                txn.database_engine,
                "user_id",
                users_to_remove,
            )
            stream_id_clause, stream_id_args = make_in_list_sql_clause(
                txn.database_engine,
                "stream_id",
                stream_ids,
            )
            sql = f"""
                DELETE FROM profile_updates_per_user
                    WHERE {user_clause}
                    AND {stream_id_clause}
            """
            params = user_args + stream_id_args
            txn.execute(sql, (*params,))

        await self.db_pool.runInteraction(
            "clear_profile_updates_for_user",
            _clear_profile_updates_for_user_txn,
        )

    @wrap_as_background_process("prune_profile_updates")
    async def _prune_profile_updates(self) -> None:
        """Delete old entries out of the `profile_updates` and
        `profile_updates_per_user` tables, so that the tables don't grow indefinitely.
        """
        prune_before_ts = self.clock.time_msec() - PRUNE_PROFILE_UPDATES_AGE.as_millis()
        cutoff_sql = """
            SELECT stream_id FROM profile_updates
            WHERE inserted_ts <= ?
            ORDER BY inserted_ts DESC
            LIMIT 1
        """

        def get_prune_before_stream_id_txn(txn: LoggingTransaction) -> int | None:
            txn.execute(cutoff_sql, (prune_before_ts,))
            row = txn.fetchone()
            return row[0] if row else None

        prune_before_stream_id = await self.db_pool.runInteraction(
            "prune_profile_updates_get_stream_id",
            get_prune_before_stream_id_txn,
        )

        if prune_before_stream_id is None:
            return

        # Get the max stream ID in the table so we avoid deleting it. We need
        # to keep the latest row so that we can calculate the maximum stream ID
        # used.
        max_stream_id = await self.db_pool.simple_select_one_onecol(
            table="profile_updates",
            keyvalues={},
            retcol="MAX(stream_id)",
            desc="prune_profile_updates_get_max_stream_id",
        )
        if prune_before_stream_id >= max_stream_id:
            prune_before_stream_id = max_stream_id - 1

        logger.debug(
            "Pruning profile_updates before stream ID %d (timestamp %d)",
            prune_before_stream_id,
            prune_before_ts,
        )
        # Now delete all rows with stream_id less than the
        # prune_before_stream_id.
        #
        # We also delete in batches to avoid massive churn when initially
        # clearing out all the old entries.
        #
        # We set a minimum stream ID so that when we delete in batches the
        # database doesn't have to scan through all the (dead) tuples that were just
        # deleted to find the next batch to delete.

        # The minimum stream ID to delete in the next batch, c.f. comment above.
        # We default to 0 here as that is less than all possible stream IDs.
        min_stream_id = 0

        def prune_profile_updates_txn(txn: LoggingTransaction) -> int:
            nonlocal min_stream_id

            assert table in ("profile_updates", "profile_updates_per_user")
            delete_sql = """
                    DELETE FROM %s
                    WHERE stream_id IN (
                        SELECT stream_id FROM %s
                        WHERE ? < stream_id AND stream_id <= ?
                        ORDER BY stream_id ASC
                        LIMIT ?
                    )
                    RETURNING stream_id
                """ % (table, table)
            txn.execute(
                delete_sql,
                (
                    min_stream_id,
                    prune_before_stream_id,
                    PRUNE_PROFILE_UPDATES_BATCH_SIZE,
                ),
            )

            # We can't use rowcount as that is incorrect on SQLite when using
            # RETURNING.
            num_deleted = 0
            for row in txn:
                num_deleted += 1
                min_stream_id = max(min_stream_id, row[0])

            return num_deleted

        # Do this twice, first for the per_user table, then for the main table
        for table in ("profile_updates_per_user", "profile_updates"):
            progress_num_rows_deleted = 0
            while True:
                batch_deleted = await self.db_pool.runInteraction(
                    f"prune_{table}",
                    prune_profile_updates_txn,
                )

                finished = batch_deleted < PRUNE_PROFILE_UPDATES_BATCH_SIZE

                progress_num_rows_deleted += batch_deleted

                # Periodically report progress in the logs. We do this either when
                # we've deleted a significant number of rows or when we've finished
                # deleting all rows in this round.
                if finished or progress_num_rows_deleted > 10000:
                    logger.info(
                        "Pruned %d rows from %s",
                        progress_num_rows_deleted,
                        table,
                    )
                    progress_num_rows_deleted = 0

                if finished:
                    break

                # Sleep for a short time to avoid hammering the database too much if
                # there are a lot of rows to delete.
                await self.clock.sleep(Duration(milliseconds=100))

            # Reset the minimum stream id for our next table
            min_stream_id = 0


class ProfileStore(ProfileWorkerStore):
    pass
