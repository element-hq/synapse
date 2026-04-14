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


-- Tracks the maximum stream_id that has been deleted (pruned) from the
-- device_lists_changes_in_room table. This is used to determine whether
-- it's safe to read from that table for a given stream_id — if the
-- requested stream_id is <= the value here, the data has been pruned and
-- the table cannot provide a complete answer.
--
-- This replaces the previous approach of using MIN(stream_id) on the
-- device_lists_changes_in_room table, which incorrectly returned 0 when
-- the table was empty.
CREATE TABLE IF NOT EXISTS device_lists_changes_in_room_max_pruned_stream_id (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,
    stream_id BIGINT NOT NULL
);

-- Seed with the current minimum stream_id minus 1, or 0 if the table is empty.
INSERT INTO device_lists_changes_in_room_max_pruned_stream_id (stream_id)
    SELECT COALESCE(MIN(stream_id) - 1, 0) FROM device_lists_changes_in_room;
