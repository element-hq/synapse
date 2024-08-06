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
    FOREIGN KEY(room_id) REFERENCES rooms(room_id),
    -- The `stream_ordering` of the latest event in the room
    event_stream_ordering BIGINT REFERENCES events(stream_ordering)
    -- The `stream_ordering` of the last event according to the `bump_event_types`
    bump_stamp: BIGINT,
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
    FOREIGN KEY(room_id) REFERENCES rooms(room_id),
    FOREIGN KEY(membership_event_id) REFERENCES events(event_id),
    user_id TEXT NOT NULL,
    membership TEXT NOT NULL,
    -- `stream_ordering` of the `membership_event_id`
    event_stream_ordering BIGINT REFERENCES events(stream_ordering)
    -- `m.room.create` -> `content.type`
    room_type TEXT,
    -- `m.room.name` -> `content.name`
    room_name TEXT,
    -- `m.room.encryption` -> `content.algorithm`
    is_encrypted BOOLEAN,
    PRIMARY KEY (room_id, membership_event_id)
);
