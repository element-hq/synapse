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

from synapse.storage._base import SQLBaseStore


class ServerAnnouncementsStore(SQLBaseStore):
    """Stores details about an announcement room.

    The unique announcement `id` identifies the same room even when its configured
    details change. It is separate from the actual room ID.

    Lookups are cheap and deliberately uncached, avoiding cross-worker cache
    invalidation for mapping and avatar changes.
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

    async def store_server_announcement_room(
        self, config_id: str, room_id: str
    ) -> None:
        """Store a new mapping."""
        await self.db_pool.simple_insert(
            table="server_announcement_rooms",
            values={"config_id": config_id, "room_id": room_id},
            desc="store_server_announcement_room",
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
