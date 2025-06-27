--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

CREATE TABLE deleted_room_members (
    room_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    deleted_at_stream_id bigint NOT NULL
);
CREATE UNIQUE INDEX deleted_room_member_idx ON deleted_room_members(room_id, user_id);
