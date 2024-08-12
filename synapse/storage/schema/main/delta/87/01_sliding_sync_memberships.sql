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

-- A table for storing room meta data (current state relevant to sliding sync) that the
-- local server is still participating in (someone local is joined to the room).
--
-- We store the joined rooms in separate table from `sliding_sync_membership_snapshots`
-- because we need up-to-date information for joined rooms and it can be shared across
-- everyone who is joined.
--
-- This table is kept in sync with `current_state_events` which means if the server is
-- no longer participating in a room, the row will be deleted.
CREATE TABLE IF NOT EXISTS sliding_sync_joined_rooms(
    room_id TEXT NOT NULL REFERENCES rooms(room_id),
    -- The `stream_ordering` of the latest event in the room
    event_stream_ordering BIGINT REFERENCES events(stream_ordering),
    -- The `stream_ordering` of the last event according to the `bump_event_types`
    bump_stamp BIGINT,
    -- `m.room.create` -> `content.type` (current state)
    room_type TEXT,
    -- `m.room.name` -> `content.name` (current state)
    room_name TEXT,
    -- `m.room.encryption` -> `content.algorithm` (current state)
    is_encrypted BOOLEAN DEFAULT 0 NOT NULL,
    -- FIXME: Maybe we want to add `tombstone_successor_room_id` here to help with `include_old_rooms`
    -- (tracked by https://github.com/element-hq/synapse/issues/17540)
    PRIMARY KEY (room_id)
);

-- A table for storing a snapshot of room meta data (historical current state relevant
-- for sliding sync) at the time of a local user's membership. Only has rows for the
-- latest membership event for a given local user in a room which matches
-- `local_current_membership` .
--
-- We store all memberships including joins. This makes it easy to reference this table
-- to find all membership for a given user and shares the same semantics as
-- `local_current_membership`. And we get to avoid some table maintenance; if we only
-- stored non-joins, we would have to delete the row for the user when the user joins
-- the room.
--
-- We don't include `bump_stamp` here because we can just use the `stream_ordering` from
-- the membership event itself as the `bump_stamp`.
CREATE TABLE IF NOT EXISTS sliding_sync_membership_snapshots(
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
    is_encrypted BOOLEAN DEFAULT 0 NOT NULL,
    -- FIXME: Maybe we want to add `tombstone_successor_room_id` here to help with `include_old_rooms`
    -- (tracked by https://github.com/element-hq/synapse/issues/17540)
    PRIMARY KEY (room_id, user_id)
);

-- So we can fetch all rooms for a given user
CREATE UNIQUE INDEX IF NOT EXISTS sliding_sync_membership_snapshots_user_id ON sliding_sync_membership_snapshots(user_id);
-- So we can sort by `stream_ordering
CREATE UNIQUE INDEX IF NOT EXISTS sliding_sync_membership_snapshots_event_stream_ordering ON sliding_sync_membership_snapshots(event_stream_ordering);
