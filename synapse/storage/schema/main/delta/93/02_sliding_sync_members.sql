--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.


-- Tracks which member states have been sent to the client for lazy-loaded
-- members in sliding sync. This is a *cache* as it doesn't matter if we send
-- down members we've previously sent down, i.e. it's safe to delete any rows.
--
-- We track a *rough* `last_seen_ts` for each user in each room which indicates
-- when we last would've sent their member state to the client. This is used so
-- that we can remove members which haven't been seen for a while to save space.
--
-- Care must be taken when handling "forked" positions, i.e. we have responded
-- to a request with a position and then get another different request using the
-- previous position as a base. We track this by including a
-- `connection_position` for newly inserted rows. When we advance the position
-- we set this to NULL for all rows which were present at that position, and
-- delete all other rows. When reading rows we can then filter out any rows
-- which have a non-NULL `connection_position` which is not the current
-- position.
--
-- I.e. `connection_position` is NULL for rows which are valid for *all*
-- positions on the connection, and is non-NULL for rows which are only valid
-- for a specific position.
--
-- When invalidating rows, we can just delete them. Technically this could
-- invalidate for a forked position, but this is acceptable as equivalent to a
-- cache eviction.
CREATE TABLE sliding_sync_connection_lazy_members (
    connection_key BIGINT NOT NULL REFERENCES sliding_sync_connections(connection_key) ON DELETE CASCADE,
    connection_position BIGINT REFERENCES sliding_sync_connection_positions(connection_position) ON DELETE CASCADE,
    room_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    last_seen_ts BIGINT NOT NULL
);

CREATE UNIQUE INDEX sliding_sync_connection_lazy_members_idx ON sliding_sync_connection_lazy_members (connection_key, room_id, user_id);
CREATE INDEX sliding_sync_connection_lazy_members_pos_idx ON sliding_sync_connection_lazy_members (connection_key, connection_position) WHERE connection_position IS NOT NULL;
