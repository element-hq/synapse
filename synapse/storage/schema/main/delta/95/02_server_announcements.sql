--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Mappings and avatar metadata are retained when announcements are removed from
-- configuration, but deleted when their Matrix room is purged. The main room
-- purge table list handles this transactionally, without a foreign key to rooms.
CREATE TABLE server_announcement_rooms (
    id TEXT PRIMARY KEY NOT NULL,
    room_id TEXT UNIQUE NOT NULL,
    avatar_path TEXT,
    avatar_sha256 TEXT,
    avatar_mxc TEXT
);
