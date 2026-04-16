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
-- device_lists_changes_in_room table. This is used to determine whether it's
-- safe to read from that table for a given stream_id — if the requested
-- stream_id is < the value here, the data has been pruned and the table cannot
-- provide a complete answer.
--
-- We need a separate table, rather than looking at the minimum stream_id in the
-- device_lists_changes_in_room table, because not all valid stream IDs will
-- have entries in the table. This could lead to situations where the minimum
-- stream ID was potentially much more recent than when we actually pruned. This
-- would cause us to incorrectly think that the table was not safe to read from,
-- when in fact it was.
CREATE TABLE IF NOT EXISTS device_lists_changes_in_room_max_pruned_stream_id (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,
    stream_id BIGINT NOT NULL
);

-- We assume that nothing has been deleted from the device_lists_changes_in_room
-- table, so we can set the initial value to 0.
INSERT INTO device_lists_changes_in_room_max_pruned_stream_id (stream_id) VALUES (0);
