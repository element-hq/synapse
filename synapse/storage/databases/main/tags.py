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

import logging
from typing import Any, Iterable, Mapping, cast

from synapse.api.constants import AccountDataTypes
from synapse.replication.tcp.streams import AccountDataStream
from synapse.storage._base import db_to_json
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.account_data import AccountDataWorkerStore
from synapse.storage.util.id_generators import AbstractStreamIdGenerator
from synapse.types import JsonDict, JsonMapping
from synapse.util.caches.descriptors import cached
from synapse.util.json import json_encoder

logger = logging.getLogger(__name__)


class TagsWorkerStore(AccountDataWorkerStore):
    @cached()
    async def get_tags_for_user(
        self, user_id: str
    ) -> Mapping[str, Mapping[str, JsonMapping]]:
        """Get all the tags for a user.


        Args:
            user_id: The user to get the tags for.
        Returns:
            A mapping from room_id strings to dicts mapping from tag strings to
            tag content.
        """

        rows = cast(
            list[tuple[str, str, str]],
            await self.db_pool.simple_select_list(
                "room_tags", {"user_id": user_id}, ["room_id", "tag", "content"]
            ),
        )

        tags_by_room: dict[str, dict[str, JsonDict]] = {}
        for room_id, tag, content in rows:
            room_tags = tags_by_room.setdefault(room_id, {})
            room_tags[tag] = db_to_json(content)
        return tags_by_room

    async def get_all_updated_tags(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> tuple[list[tuple[int, str, str]], int, bool]:
        """Get updates for tags replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updatees.

            The updates are a list of tuples of stream ID, user ID and room ID
        """

        if last_id == current_id:
            return [], current_id, False

        def get_all_updated_tags_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[int, str, str]]:
            sql = (
                "SELECT stream_id, user_id, room_id"
                " FROM room_tags_revisions as r"
                " WHERE ? < stream_id AND stream_id <= ?"
                " ORDER BY stream_id ASC LIMIT ?"
            )
            txn.execute(sql, (last_id, current_id, limit))
            # mypy doesn't understand what the query is selecting.
            return cast(list[tuple[int, str, str]], txn.fetchall())

        tag_ids = await self.db_pool.runInteraction(
            "get_all_updated_tags", get_all_updated_tags_txn
        )

        limited = False
        upto_token = current_id
        if len(tag_ids) >= limit:
            upto_token = tag_ids[-1][0]
            limited = True

        return tag_ids, upto_token, limited

    async def get_updated_tags(
        self, user_id: str, stream_id: int
    ) -> Mapping[str, Mapping[str, JsonMapping]]:
        """Get all the tags for the rooms where the tags have changed since the
        given version

        Args:
            user_id: The user to get the tags for.
            stream_id: The earliest update to get for the user.

        Returns:
            A mapping from room_id strings to lists of tag strings for all the
            rooms that changed since the stream_id token.
        """

        def get_updated_tags_txn(txn: LoggingTransaction) -> list[str]:
            sql = (
                "SELECT room_id from room_tags_revisions"
                " WHERE user_id = ? AND stream_id > ?"
            )
            txn.execute(sql, (user_id, stream_id))
            room_ids = [row[0] for row in txn]
            return room_ids

        changed = self._account_data_stream_cache.has_entity_changed(
            user_id, int(stream_id)
        )
        if not changed:
            return {}

        room_ids = await self.db_pool.runInteraction(
            "get_updated_tags", get_updated_tags_txn
        )

        results = {}
        if room_ids:
            tags_by_room = await self.get_tags_for_user(user_id)
            for room_id in room_ids:
                results[room_id] = tags_by_room.get(room_id, {})

        return results

    async def has_tags_changed_for_room(
        self,
        # Since there are multiple arguments with the same type, force keyword arguments
        # so people don't accidentally swap the order
        *,
        user_id: str,
        room_id: str,
        from_stream_id: int,
        to_stream_id: int,
    ) -> bool:
        """Check if the users tags for a room have been updated in the token range

        (> `from_stream_id` and <= `to_stream_id`)

        Args:
            user_id: The user to get tags for
            room_id: The room to get tags for
            from_stream_id: The point in the stream to fetch from
            to_stream_id: The point in the stream to fetch to

        Returns:
            A mapping of tags to tag content.
        """

        # Shortcut if no room has changed for the user
        changed = self._account_data_stream_cache.has_entity_changed(
            user_id, int(from_stream_id)
        )
        if not changed:
            return False

        last_change_position_for_room = await self.db_pool.simple_select_one_onecol(
            table="room_tags_revisions",
            keyvalues={"user_id": user_id, "room_id": room_id},
            retcol="stream_id",
            allow_none=True,
        )

        if last_change_position_for_room is None:
            return False

        return (
            last_change_position_for_room > from_stream_id
            and last_change_position_for_room <= to_stream_id
        )

    @cached(num_args=2, tree=True)
    async def get_tags_for_room(
        self, user_id: str, room_id: str
    ) -> Mapping[str, JsonMapping]:
        """Get all the tags for the given room

        Args:
            user_id: The user to get tags for
            room_id: The room to get tags for

        Returns:
            A mapping of tags to tag content.
        """
        rows = cast(
            list[tuple[str, str]],
            await self.db_pool.simple_select_list(
                table="room_tags",
                keyvalues={"user_id": user_id, "room_id": room_id},
                retcols=("tag", "content"),
                desc="get_tags_for_room",
            ),
        )
        return {tag: db_to_json(content) for tag, content in rows}

    async def add_tag_to_room(
        self, user_id: str, room_id: str, tag: str, content: JsonMapping
    ) -> int:
        """Add a tag to a room for a user.

        Args:
            user_id: The user to add a tag for.
            room_id: The room to add a tag for.
            tag: The tag name to add.
            content: A json object to associate with the tag.

        Returns:
            The next account data ID.
        """
        assert self._can_write_to_account_data
        assert isinstance(self._account_data_id_gen, AbstractStreamIdGenerator)

        content_json = json_encoder.encode(content)

        def add_tag_txn(txn: LoggingTransaction, next_id: int) -> None:
            self.db_pool.simple_upsert_txn(
                txn,
                table="room_tags",
                keyvalues={"user_id": user_id, "room_id": room_id, "tag": tag},
                values={"content": content_json},
            )
            self._update_revision_txn(txn, user_id, room_id, next_id)

        async with self._account_data_id_gen.get_next() as next_id:
            await self.db_pool.runInteraction("add_tag", add_tag_txn, next_id)

        self.get_tags_for_user.invalidate((user_id,))
        self.get_tags_for_room.invalidate((user_id, room_id))

        return self._account_data_id_gen.get_current_token()

    async def remove_tag_from_room(self, user_id: str, room_id: str, tag: str) -> int:
        """Remove a tag from a room for a user.

        Returns:
            The next account data ID.
        """
        assert self._can_write_to_account_data
        assert isinstance(self._account_data_id_gen, AbstractStreamIdGenerator)

        def remove_tag_txn(txn: LoggingTransaction, next_id: int) -> None:
            sql = "DELETE FROM room_tags WHERE user_id = ? AND room_id = ? AND tag = ?"
            txn.execute(sql, (user_id, room_id, tag))
            self._update_revision_txn(txn, user_id, room_id, next_id)

        async with self._account_data_id_gen.get_next() as next_id:
            await self.db_pool.runInteraction("remove_tag", remove_tag_txn, next_id)

        self.get_tags_for_user.invalidate((user_id,))
        self.get_tags_for_room.invalidate((user_id, room_id))

        return self._account_data_id_gen.get_current_token()

    def _update_revision_txn(
        self, txn: LoggingTransaction, user_id: str, room_id: str, next_id: int
    ) -> None:
        """Update the latest revision of the tags for the given user and room.

        Args:
            txn: The database cursor
            user_id: The ID of the user.
            room_id: The ID of the room.
            next_id: The the revision to advance to.
        """
        assert self._can_write_to_account_data
        assert isinstance(self._account_data_id_gen, AbstractStreamIdGenerator)

        txn.call_after(
            self._account_data_stream_cache.entity_has_changed, user_id, next_id
        )

        update_sql = (
            "UPDATE room_tags_revisions"
            " SET stream_id = ?"
            " WHERE user_id = ?"
            " AND room_id = ?"
        )
        txn.execute(update_sql, (next_id, user_id, room_id))

        if txn.rowcount == 0:
            insert_sql = (
                "INSERT INTO room_tags_revisions (user_id, room_id, stream_id)"
                " VALUES (?, ?, ?)"
            )
            try:
                txn.execute(insert_sql, (user_id, room_id, next_id))
            except self.database_engine.module.IntegrityError:
                # Ignore insertion errors. It doesn't matter if the row wasn't
                # inserted because if two updates happend concurrently the one
                # with the higher stream_id will not be reported to a client
                # unless the previous update has completed. It doesn't matter
                # which stream_id ends up in the table, as long as it is higher
                # than the id that the client has.
                pass

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        if stream_name == AccountDataStream.NAME:
            # Cast is safe because the `AccountDataStream` should only be giving us
            # `AccountDataStreamRow`
            account_data_stream_rows: list[AccountDataStream.AccountDataStreamRow] = (
                cast(list[AccountDataStream.AccountDataStreamRow], rows)
            )

            for row in account_data_stream_rows:
                if row.data_type == AccountDataTypes.TAG:
                    self.get_tags_for_user.invalidate((row.user_id,))
                    if row.room_id:
                        self.get_tags_for_room.invalidate((row.user_id, row.room_id))
                    else:
                        self.get_tags_for_room.invalidate((row.user_id,))
                    self._account_data_stream_cache.entity_has_changed(
                        row.user_id, token
                    )

        super().process_replication_rows(stream_name, instance_name, token, rows)


class TagsStore(TagsWorkerStore):
    pass
