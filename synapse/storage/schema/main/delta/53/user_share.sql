--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2023 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.
--
-- Originally licensed under the Apache License, Version 2.0:
-- <http://www.apache.org/licenses/LICENSE-2.0>.
--
-- [This file includes modifications made by New Vector Limited]
--
--
/* Copyright 2017 Vector Creations Ltd, 2019 New Vector Ltd
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

-- Old disused version of the tables below.
DROP TABLE IF EXISTS users_who_share_rooms;

-- Tables keeping track of what users share rooms. This is a map of local users
-- to local or remote users, per room. Remote users cannot be in the user_id
-- column, only the other_user_id column. There are two tables, one for public
-- rooms and those for private rooms.
CREATE TABLE IF NOT EXISTS users_who_share_public_rooms (
    user_id TEXT NOT NULL,
    other_user_id TEXT NOT NULL,
    room_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users_who_share_private_rooms (
    user_id TEXT NOT NULL,
    other_user_id TEXT NOT NULL,
    room_id TEXT NOT NULL
);

CREATE UNIQUE INDEX users_who_share_public_rooms_u_idx ON users_who_share_public_rooms(user_id, other_user_id, room_id);
CREATE INDEX users_who_share_public_rooms_r_idx ON users_who_share_public_rooms(room_id);
CREATE INDEX users_who_share_public_rooms_o_idx ON users_who_share_public_rooms(other_user_id);

CREATE UNIQUE INDEX users_who_share_private_rooms_u_idx ON users_who_share_private_rooms(user_id, other_user_id, room_id);
CREATE INDEX users_who_share_private_rooms_r_idx ON users_who_share_private_rooms(room_id);
CREATE INDEX users_who_share_private_rooms_o_idx ON users_who_share_private_rooms(other_user_id);

-- Make sure that we populate the tables initially by resetting the stream ID
UPDATE user_directory_stream_pos SET stream_id = NULL;
