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

-- A mapping of configured unique `config_id` of a server announcement room, and
-- its actual room ID. 
--
-- Also stores details about its room avatar. When the room avatar is sourced
-- from disk, this information is referenced on each startup and the image is
-- re-uploaded if the hash changes.
CREATE TABLE server_announcement_rooms (
    config_id TEXT PRIMARY KEY NOT NULL,
    room_id TEXT UNIQUE NOT NULL,
    avatar_path TEXT,
    avatar_sha256 TEXT,
    avatar_mxc TEXT
);
