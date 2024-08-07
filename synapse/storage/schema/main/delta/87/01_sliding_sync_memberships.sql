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

-- We store the join memberships in a separate table from
-- `sliding_sync_non_join_memberships` because the information can be shared across
-- everyone who is joined.
--
-- This table is kept in sync with `current_state_events`
CREATE TABLE IF NOT EXISTS sliding_sync_joined_rooms(
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    -- The `stream_ordering` of the latest event in the room
    event_stream_ordering BIGINT REFERENCES events(stream_ordering),
    -- The `stream_ordering` of the last event according to the `bump_event_types`
    bump_stamp BIGINT,
    -- `m.room.create` -> `content.type`
    room_type TEXT,
    -- `m.room.name` -> `content.name`
    room_name TEXT,
    -- `m.room.encryption` -> `content.algorithm`
    is_encrypted BOOLEAN,
    PRIMARY KEY (room_id)
);


-- We don't include `bump_stamp` here because we can just use the `stream_ordering` from
-- the membership event itself as the `bump_stamp`.
CREATE TABLE IF NOT EXISTS sliding_sync_non_join_memberships(
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    user_id TEXT NOT NULL,
    membership_event_id TEXT NOT NULL REFERENCES events(event_id),
    membership TEXT NOT NULL,
    -- `stream_ordering` of the `membership_event_id`
    event_stream_ordering BIGINT REFERENCES events(stream_ordering),
    -- `m.room.create` -> `content.type` (according to the current state at the time of
    -- the membership)
    room_type TEXT,
    -- `m.room.name` -> `content.name` (according to the current state at the time of
    -- the membership)
    room_name TEXT,
    -- `m.room.encryption` -> `content.algorithm` (according to the current state at the
    -- time of the membership)
    is_encrypted BOOLEAN,
    PRIMARY KEY (room_id, user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS sliding_sync_non_join_memberships_event_stream_ordering ON sliding_sync_non_join_memberships(event_stream_ordering);
CREATE UNIQUE INDEX IF NOT EXISTS sliding_sync_non_join_memberships_membership_event_id ON sliding_sync_non_join_memberships(membership_event_id);
