--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd.
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Stores what profile field data we have sent for given users down given connections.
--
-- Similar to `sliding_sync_connection_streams`, but for user profile fields rather than rooms.
-- This tracks which profile fields we've sent for which users on a given connection position.
CREATE TABLE sliding_sync_connection_profile_updates(
    connection_position BIGINT NOT NULL REFERENCES sliding_sync_connection_positions(connection_position) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    field_status TEXT NOT NULL,  -- "live" or "previously", i.e. the `HaveSentRoomFlag` value
    last_token TEXT  -- For "previously" the token for the stream we have sent up to.
);

CREATE UNIQUE INDEX sliding_sync_connection_profile_updates_idx ON sliding_sync_connection_profile_updates(connection_position, user_id, field_name);