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

-- Add indexes to improve performance of the thread_updates endpoint and
-- sliding sync threads extension (MSC4360).

-- Index for efficiently finding all events that relate to a specific event
-- (e.g., all replies to a thread root). This is used by the correlated subquery
-- in get_thread_updates_for_user that counts thread updates.
-- Also useful for other relation queries (edits, reactions, etc.).
CREATE INDEX IF NOT EXISTS event_relations_relates_to_id_type
    ON event_relations(relates_to_id, relation_type);

-- Index for the /thread_updates endpoint's cross-room query.
-- Allows efficient descending ordering and range filtering of threads
-- by stream_ordering across all rooms.
CREATE INDEX IF NOT EXISTS threads_stream_ordering_desc
    ON threads(stream_ordering DESC);

-- Index for the EXISTS clause that filters threads to only joined rooms.
-- Allows efficient lookup of a user's current room memberships.
CREATE INDEX IF NOT EXISTS local_current_membership_user_room
    ON local_current_membership(user_id, membership, room_id);
