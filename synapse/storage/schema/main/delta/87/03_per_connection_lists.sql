--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2024 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.


-- Stores the room lists for a connection
CREATE TABLE sliding_sync_connection_room_lists(
    connection_key BIGINT NOT NULL REFERENCES sliding_sync_connections(connection_key) ON DELETE CASCADE,
    list_name TEXT NOT NULL,
    room_id TEXT NOT NULL
);

CREATE INDEX sliding_sync_connection_room_lists_idx ON sliding_sync_connection_room_lists(connection_key);
