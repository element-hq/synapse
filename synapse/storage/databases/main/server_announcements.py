#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import Collection

from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.types import PersistedEventPosition
from synapse.util.caches.descriptors import cached


class ServerAnnouncementsStore(CacheInvalidationWorkerStore):
    """Stores details about an announcement room.

    The unique announcement `id` identifies the same room even when its configured
    details change. It is separate from the actual room ID.

    Only the set of configured room IDs is cached, as it is read on every sync.
    """

    async def get_server_announcement_room(self, config_id: str) -> str | None:
        """Return the room for an announcement ID, if one has been stored."""
        return await self.db_pool.simple_select_one_onecol(
            table="server_announcement_rooms",
            keyvalues={"config_id": config_id},
            retcol="room_id",
            allow_none=True,
            desc="get_server_announcement_room",
        )

    async def get_server_announcement_id(self, room_id: str) -> str | None:
        """Return the announcement ID for a room, if one has been stored."""
        return await self.db_pool.simple_select_one_onecol(
            table="server_announcement_rooms",
            keyvalues={"room_id": room_id},
            retcol="config_id",
            allow_none=True,
            desc="get_server_announcement_id",
        )

    @cached(num_args=0)
    async def get_server_announcement_room_ids(self) -> frozenset[str]:
        """Return the room IDs of the currently configured announcement rooms.

        Rooms whose config ID is no longer in the config are left out, as
        are configured rooms that have not been created yet.
        """
        rows = await self.db_pool.simple_select_many_batch(
            table="server_announcement_rooms",
            column="config_id",
            iterable=[
                room.config_id for room in self.hs.config.serverannouncements.rooms
            ],
            retcols=("room_id",),
            desc="get_server_announcement_room_ids",
        )
        return frozenset(room_id for (room_id,) in rows)

    async def get_first_non_state_event_positions(
        self, room_ids: Collection[str], max_stream_ordering: int
    ) -> dict[str, PersistedEventPosition]:
        """Return the position of the first non-state event at or before
        `max_stream_ordering` in each of `room_ids`, leaving out rooms that have
        none.
        """

        def _get_first_non_state_event_positions_txn(
            txn: LoggingTransaction,
        ) -> dict[str, PersistedEventPosition]:
            positions = {}
            for room_id in room_ids:
                txn.execute(
                    """
                    SELECT instance_name, stream_ordering FROM events
                    WHERE room_id = ? AND stream_ordering <= ?
                    AND state_key IS NULL
                    ORDER BY stream_ordering
                    LIMIT 1
                    """,
                    (room_id, max_stream_ordering),
                )
                row = txn.fetchone()
                if row is not None:
                    instance_name, stream_ordering = row
                    positions[room_id] = PersistedEventPosition(
                        instance_name or "master", stream_ordering
                    )
            return positions

        if not room_ids:
            return {}

        return await self.db_pool.runInteraction(
            "get_first_non_state_event_positions",
            _get_first_non_state_event_positions_txn,
        )

    async def store_server_announcement_room(
        self, config_id: str, room_id: str
    ) -> None:
        """Store a new mapping."""

        def _store_server_announcement_room_txn(txn: LoggingTransaction) -> None:
            self.db_pool.simple_insert_txn(
                txn,
                table="server_announcement_rooms",
                values={"config_id": config_id, "room_id": room_id},
            )
            self._invalidate_cache_and_stream(
                txn, self.get_server_announcement_room_ids, ()
            )

        await self.db_pool.runInteraction(
            "store_server_announcement_room", _store_server_announcement_room_txn
        )

    async def get_server_announcement_avatar(
        self, config_id: str
    ) -> tuple[str, str, str] | None:
        """Return the cached avatar's (path, SHA-256 hex digest, MXC URI), if complete."""
        row = await self.db_pool.simple_select_one(
            table="server_announcement_rooms",
            keyvalues={"config_id": config_id},
            retcols=("avatar_path", "avatar_sha256", "avatar_mxc"),
            allow_none=True,
            desc="get_server_announcement_avatar",
        )
        if row is None:
            return None

        path, sha256, mxc = row
        if path is None or sha256 is None or mxc is None:
            return None

        return path, sha256, mxc

    async def store_server_announcement_avatar(
        self, config_id: str, path: str, sha256: str, mxc: str
    ) -> None:
        """Store an uploaded avatar for an existing announcement room mapping.

        The hash lets us detect changes to the file on disk, even when its path is
        unchanged, and decide whether to upload it again.
        """
        await self.db_pool.simple_update_one(
            table="server_announcement_rooms",
            keyvalues={"config_id": config_id},
            updatevalues={
                "avatar_path": path,
                "avatar_sha256": sha256,
                "avatar_mxc": mxc,
            },
            desc="store_server_announcement_avatar",
        )
