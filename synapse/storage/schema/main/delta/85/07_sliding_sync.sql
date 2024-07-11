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

-- A table that maps from room ID to metadata useful for sliding sync.
CREATE TABLE sliding_sync_room_metadata (
    room_id TEXT NOT NULL PRIMARY KEY,

    -- The instance_name / stream ordering of the last event in the room.
    instance_name TEXT NOT NULL,
    last_stream_ordering BIGINT NOT NULL
);

INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (8507, 'sliding_sync_room_metadata', '{}');
