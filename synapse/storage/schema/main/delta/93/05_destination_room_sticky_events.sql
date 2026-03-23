--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.


-- Tracks rooms that have outstanding sticky events that still need to be
-- sent to a destination (remote homeserver).
--
-- Essentially a queue of unsent sticky events, but with a compact storage
-- representation that only needs one row per (destination, room) pair.
--
-- Each row means: in the given room and for the given destination,
-- we still need to send sticky events with `stream_id` at or higher than
-- `sticky_events_stream_position`.
--
-- Due to sticky event expiration and event deletion, rows in this table are
-- hints that there *may* be sticky events to send, but not a guarantee that
-- there actually are.
CREATE TABLE destination_room_sticky_events_backlog (
    -- Server name of the remote homeserver.
    destination TEXT NOT NULL,

    -- Room ID in which sticky events have been missed.
    room_id TEXT NOT NULL
        -- Only track this information for rooms we know about;
        -- if we delete a room locally then also delete the tracking info.
        REFERENCES rooms(room_id) ON DELETE CASCADE,

    -- Position in the sticky events stream, corresponding to the
    -- `sticky_events.stream_id` of the first sticky event that
    -- has yet to be sent.
    --
    -- Because sticky events must be sent in order (as per MSC4354),
    -- all subsequent sticky events for the same room with higher
    -- `stream_id`s are also unsent.
    --
    -- Not a foreign key because we must still support expiration of sticky events.
    sticky_events_stream_position INTEGER NOT NULL,

    -- It's enough to track one position per (destination, room_id) pair.
    PRIMARY KEY (destination, room_id)
);

-- Should have this index to make `room_id` foreign key constraint efficient,
-- as well as for cleanup per room.
CREATE INDEX destination_room_unsent_sticky_events_room_id ON destination_room_unsent_sticky_events (room_id);
