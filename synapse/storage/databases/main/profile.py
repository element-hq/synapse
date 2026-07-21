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
from typing import TYPE_CHECKING, Collection, cast

import attr
from canonicaljson import encode_canonical_json

from synapse.api.constants import (
    EventTypes,
    Membership,
    ProfileFields,
    ProfileUpdateAction,
)
from synapse.api.errors import Codes, StoreError
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

if TYPE_CHECKING:
    from synapse.server import HomeServer


# The number of bytes that the serialized profile can have.
MAX_PROFILE_SIZE = 65536


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

        self._msc4429_enabled = hs.config.server.include_profile_updates_in_sync
        self._is_events_writer = self._instance_name in hs.config.worker.writers.events
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
            writers=hs.config.worker.writers.events,
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
            txn.execute(
                """
                SELECT user_id FROM profiles
                WHERE user_id > ?
                ORDER BY user_id
                LIMIT 1 OFFSET 1000
                """,
                (lower_bound_id,),
            )
            res = txn.fetchone()
            if res:
                upper_bound_id = res[0]
                return upper_bound_id
            else:
                return None

        def _process_batch(
            txn: LoggingTransaction, lower_bound_id: str, upper_bound_id: str
        ) -> None:
            txn.execute(
                """
                UPDATE profiles
                SET full_user_id = '@' || user_id || ?
                WHERE ? < user_id AND user_id <= ? AND full_user_id IS NULL
                """,
                (f":{self.server_name}", lower_bound_id, upper_bound_id),
            )

        def _final_batch(txn: LoggingTransaction, lower_bound_id: str) -> None:
            txn.execute(
                """
                UPDATE profiles
                SET full_user_id = '@' || user_id || ?
                WHERE ? < user_id AND full_user_id IS NULL
                """,
                (
                    f":{self.server_name}",
                    lower_bound_id,
                ),
            )

            if isinstance(self.database_engine, PostgresEngine):
                txn.execute(
                    """
                    ALTER TABLE profiles VALIDATE CONSTRAINT full_user_id_not_null
                    """,
                )

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
                txn.execute(
                    """
                    SELECT JSONB_PATH_EXISTS(fields, ?), JSONB_EXTRACT_PATH(fields, ?)
                    FROM profiles
                    WHERE user_id = ?
                    """,
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
                txn.execute(
                    """
                    SELECT JSON_TYPE(fields, ?), JSON_EXTRACT(fields, ?)
                    FROM profiles
                    WHERE user_id = ?
                    """,
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
        # The SQLite driver doesn't have a JSON datatype.
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
            txn.execute(
                """
                SELECT
                    stream_id, user_id, action, field_name
                FROM profile_updates
                WHERE
                    ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC LIMIT ?
                """,
                (from_id, to_id, limit),
            )
            return cast(list[tuple[int, str, str, str | None]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_updated_profile_updates", _get_updated_profile_updates_txn
        )

    async def get_profile_updates_for_fields(
        self,
        *,
        from_id: int,
        to_id: int,
        field_names: Collection[str],
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

        if not field_names:
            return []

        def _get_profile_updates_for_fields_txn(
            txn: LoggingTransaction,
        ) -> list[ProfileUpdate]:
            clause, args = make_in_list_sql_clause(
                txn.database_engine, "field_name", field_names
            )
            txn.execute(
                f"""
                SELECT stream_id, user_id, action, field_name
                    FROM profile_updates
                WHERE ? < stream_id AND stream_id <= ?
                    AND ({clause} OR action != ?)
                ORDER BY stream_id ASC
                """,
                (from_id, to_id, *args, ProfileUpdateAction.UPDATE.value),
            )
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
        field_names_empty_means_all_fields: bool = False,
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
            field_names_empty_means_all_fields: If `field_names` is empty and this
                is `True`, include all field names. Defaults to `False`.
            include_users: If given, only include updates for these user IDs.

        Returns:
            A list of ProfileUpdates update rows.
        """
        if from_id >= to_id:
            return []

        if not field_names_empty_means_all_fields and len(field_names) == 0:
            return []

        if include_users is not None and len(include_users) == 0:
            # All updates have been filtered out by lazy-loading.
            return []

        def _get_profile_updates_for_user_and_fields_txn(
            txn: LoggingTransaction,
        ) -> list[ProfileUpdate]:
            if field_names_empty_means_all_fields:
                field_clause = "pu.field_name NOT NULL"
                field_args: list[str] = []
            else:
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
            txn.execute(
                f"""
                SELECT pu.stream_id, pu.user_id, pu.action, pu.field_name
                FROM profile_updates AS pu
                    INNER JOIN profile_updates_per_user AS puf
                    ON pu.stream_id = puf.stream_id
                WHERE ? < pu.stream_id AND pu.stream_id <= ?
                    AND puf.user_id = ?
                    {user_clause}
                    AND ({field_clause} OR pu.action != ?)
                ORDER BY pu.stream_id ASC
                """,
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
            # The SQLite driver doesn't have a JSON datatype.
            if isinstance(self.database_engine, Sqlite3Engine) and fields:
                user_fields = json.loads(fields)
            base_fields = {
                ProfileFields.DISPLAYNAME: displayname,
                ProfileFields.AVATAR_URL: avatar_url,
            }
            user_fields.update(base_fields)

            results[full_user_id] = user_fields

        return results

    async def create_profile(self, user_id: UserID) -> None:
        """
        Create a blank profile for a user, if one does not already exist.

        Args:
            user_id: The user to create the profile for.
        """
        user_localpart = user_id.localpart
        await self.db_pool.simple_upsert(
            table="profiles",
            keyvalues={"full_user_id": user_id.to_string()},
            values={},
            insertion_values={"user_id": user_localpart},
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

    def _set_profile_field_txn(
        self,
        txn: LoggingTransaction,
        user_id: UserID,
        field_name: str,
        new_value: JsonValue | dict[str, JsonValue],
    ) -> int | None:
        """
        Wrapper function to set a profile field value and write to the profile
        update stream tables in one transaction.

        Args:
            txn: The transaction to use
            user_id: The user to set the profile field for
            field_name: The field to set the value for
            new_value: New value for the profile field

        Returns:
            The profile updates stream ID that was created in this transaction
        """
        if self._msc4429_enabled:
            assert self._is_events_writer

        self._check_profile_size(txn, user_id, field_name, new_value)

        if field_name in (ProfileFields.DISPLAYNAME, ProfileFields.AVATAR_URL):
            self.db_pool.simple_upsert_txn(
                txn,
                table="profiles",
                keyvalues={"user_id": user_id.localpart},
                values={
                    field_name: new_value,
                    "full_user_id": user_id.to_string(),
                },
            )
        else:
            # Encode to canonical JSON.
            canonical_value = encode_canonical_json(new_value)

            if isinstance(self.database_engine, PostgresEngine):
                from psycopg2.extras import Json

                # Note that the || jsonb operator is not recursive, any duplicate
                # keys will be taken from the second value.
                txn.execute(
                    """
                    INSERT INTO profiles
                        (user_id, full_user_id, fields) VALUES (?, ?, JSON_BUILD_OBJECT(?, ?::jsonb))
                    ON CONFLICT (user_id)
                        DO UPDATE SET full_user_id = EXCLUDED.full_user_id, fields = COALESCE(profiles.fields, '{}'::jsonb) || EXCLUDED.fields
                    """,
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
                # This will error if field_name has double quotes in it, but that's not
                # possible due to the grammar.
                json_field_name = f'$."{field_name}"'

                txn.execute(
                    # You may be tempted to use json_patch instead of providing the parameters
                    # twice, but that recursively merges objects instead of replacing.
                    """
                    INSERT INTO profiles (user_id, full_user_id, fields) VALUES (?, ?, JSON_OBJECT(?, JSON(?)))
                    ON CONFLICT (user_id)
                        DO UPDATE SET full_user_id = EXCLUDED.full_user_id, fields = JSON_SET(COALESCE(profiles.fields, '{}'), ?, JSON(?))
                    """,
                    (
                        user_id.localpart,
                        user_id.to_string(),
                        json_field_name,
                        canonical_value,
                        json_field_name,
                        canonical_value,
                    ),
                )

        if not self._msc4429_enabled:
            return None

        # Record updates in the profile updates stream
        stream_id = self.record_profile_updates_txn(
            txn=txn,
            user_id=user_id,
            action=ProfileUpdateAction.UPDATE,
            field_names=[field_name],
        )

        return stream_id

    def record_profile_updates_for_user_joined_room_txn(
        self, *, txn: LoggingTransaction, room_id: str, joined_users: set[str]
    ) -> None:
        """
        Record profile updates for membership additions to a room.

        Currently, updates are only recorded for local users.

        Args:
            txn: The transaction to use.
            room_id: The room ID concerned.
            joined_users: A list of users who have "joined" the room, which here also
                means "invited" or "knocked", as in either case we consider that the
                users profile should be pushed to the client, should they need it
                already even if the user hasn't actually joined the room.
        """
        if not self._msc4429_enabled:
            return

        assert self._is_events_writer

        # Ensure we're working with local users only
        users = {user_id for user_id in joined_users if self.hs.is_mine_id(user_id)}

        # Record the profile updates for each user
        for user_id in users:
            self.record_profile_updates_txn(
                txn=txn,
                user_id=UserID.from_string(user_id),
                action=ProfileUpdateAction.JOINED_ROOM,
                field_names=None,
                user_rooms={room_id},
            )

    def record_profile_updates_txn(
        self,
        *,
        txn: LoggingTransaction,
        user_id: UserID,
        action: ProfileUpdateAction,
        field_names: list[str] | None,
        user_rooms: set[str] | None = None,
        target_users: set[str] | None = None,
    ) -> int | None:
        """
        Record updates into the profile updates stream tables.

        Currently, updates are only recorded for local users.

        Args:
            txn: Transaction to use
            user_id: User ID that made the profile update
            action: The profile update action, either `update`, `left_room` or
                `joined_room`
            field_names: A list of fields that were set, if ProfileUpdateAction.UPDATE
            user_rooms: Optionally, a set of rooms that the update concerns. If not
                given, a database lookup will be done to fetch all the users rooms.
            target_users: Optionally, set of users to create profile update stream rows
                for. If not given, a database lookup will be done based on `user_rooms`,
                or if that is not set, the result of the rooms lookup.

        Returns:
            The latest stream ID created in this transaction
        """
        if not self._msc4429_enabled:
            return None

        if action == ProfileUpdateAction.UPDATE:
            assert field_names
        else:
            assert not field_names

        if not target_users:
            if not user_rooms:
                rows = self.db_pool.simple_select_onecol_txn(
                    txn=txn,
                    table="current_state_events",
                    keyvalues={
                        "type": EventTypes.Member,
                        "membership": Membership.JOIN,
                        "state_key": user_id.to_string(),
                    },
                    retcol="room_id",
                )
                user_rooms = set(rows)

            rows = self.db_pool.simple_select_many_txn(
                txn=txn,
                table="local_current_membership",
                column="room_id",
                iterable=user_rooms,
                retcols=("user_id",),
                keyvalues={
                    "membership": Membership.JOIN,
                },
            )
            target_users = {row[0] for row in rows}

        # Ensure we only write updates for local users
        users = {user for user in target_users if self.hs.is_mine_id(user)}

        if action in (ProfileUpdateAction.JOINED_ROOM, ProfileUpdateAction.LEFT_ROOM):
            users.discard(user_id.to_string())
            if not users:
                # No point writing an update for ourselves, if a membership change and no
                # other users interested
                return None
        elif action == ProfileUpdateAction.UPDATE:
            # Always include ourselves when updating field values
            users.add(user_id.to_string())

        # Record the profile update
        inserted_ts = self.clock.time_msec()
        if field_names:
            stream_ids = self._profile_updates_id_gen.get_next_mult_txn(
                txn, len(field_names)
            )
            values: list[tuple[int, str, str, str, str | None, int]] = [
                (
                    stream_id,
                    self._instance_name,
                    user_id.to_string(),
                    action.value,
                    field_name,
                    inserted_ts,
                )
                for stream_id, field_name in zip(stream_ids, field_names)
            ]
        else:
            stream_ids = [self._profile_updates_id_gen.get_next_txn(txn)]
            values = [
                (
                    stream_ids[0],
                    self._instance_name,
                    user_id.to_string(),
                    action.value,
                    None,
                    inserted_ts,
                )
            ]
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

        # Add per user tracking rows for each generated stream ID
        inserted_ts = self.clock.time_msec()
        per_user_values = [
            (stream_id, user_id, inserted_ts)
            for user_id in users
            for stream_id in stream_ids
        ]
        self.db_pool.simple_insert_many_txn(
            txn,
            table="profile_updates_per_user",
            keys=[
                "stream_id",
                "user_id",
                "inserted_ts",
            ],
            values=per_user_values,
        )
        return stream_ids[-1]

    async def set_profile_field(
        self,
        user_id: UserID,
        field_name: str,
        new_value: JsonValue | dict[str, JsonValue],
    ) -> int | None:
        """
        Set a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The name of the custom profile field.
            new_value: The value of the custom profile field.
        """
        return await self.db_pool.runInteraction(
            "set_profile_field",
            self._set_profile_field_txn,
            user_id,
            field_name,
            new_value,
        )

    async def delete_profile_field(
        self,
        user_id: UserID,
        field_name: str,
    ) -> int | None:
        """
        Remove a custom profile field for a user.

        Args:
            user_id: The user's ID.
            field_name: The name of the custom profile field.
        """

        if self._msc4429_enabled:
            assert self._is_events_writer

        def delete_profile_field(txn: LoggingTransaction) -> int | None:
            if isinstance(self.database_engine, PostgresEngine):
                txn.execute(
                    """
                    UPDATE profiles SET fields = fields - ?
                    WHERE user_id = ?
                    """,
                    (field_name, user_id.localpart),
                )
            else:
                txn.execute(
                    """
                    UPDATE profiles SET fields = json_remove(fields, ?)
                    WHERE user_id = ?
                    """,
                    # This will error if field_name has double quotes in it.
                    (f'$."{field_name}"', user_id.localpart),
                )

            if not self._msc4429_enabled:
                return None

            stream_id = self.record_profile_updates_txn(
                txn=txn,
                user_id=user_id,
                action=ProfileUpdateAction.UPDATE,
                field_names=[field_name],
            )
            return stream_id

        return await self.db_pool.runInteraction(
            "delete_profile_field", delete_profile_field
        )

    async def delete_profile(
        self,
        user_id: UserID,
    ) -> None:
        """
        Deletes an entire user profile, including displayname, avatar_url and all
        custom fields. Used at user deactivation when erasure is requested.

        Args:
            user_id: User ID whose profile is going to be deleted.
        """

        def _delete_profile_txn(txn: LoggingTransaction) -> None:
            # Delete the profile
            txn.execute(
                """
                DELETE FROM profiles
                WHERE full_user_id = ?
                """,
                (user_id.to_string(),),
            )

        await self.db_pool.runInteraction(
            "delete_profile",
            _delete_profile_txn,
        )


class ProfileStore(ProfileWorkerStore):
    pass
