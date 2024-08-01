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

CREATE TABLE IF NOT EXISTS sliding_sync_joined_rooms(
    FOREIGN KEY(room_id) REFERENCES rooms(room_id),
    room_type TEXT,
    room_name TEXT,
    is_encrypted BOOLEAN,
    stream_ordering: BIGINT,
    bump_stamp: BIGINT,
);

CREATE UNIQUE INDEX IF NOT EXISTS sliding_sync_joined_rooms_room_id ON sliding_sync_joined_rooms(room_id);

CREATE TABLE IF NOT EXISTS sliding_sync_non_join_memberships(
    FOREIGN KEY(membership_event_id) REFERENCES events(event_id),
    FOREIGN KEY(room_id) REFERENCES rooms(room_id),
    room_type TEXT,
    room_name TEXT,
    is_encrypted BOOLEAN,
    stream_ordering: BIGINT,
    bump_stamp: BIGINT,
);

CREATE UNIQUE INDEX IF NOT EXISTS sliding_sync_non_join_memberships_membership_event_id ON sliding_sync_non_join_memberships(membership_event_id);
