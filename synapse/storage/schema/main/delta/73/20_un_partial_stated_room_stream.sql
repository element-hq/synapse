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
/* Copyright 2022 The Matrix.org Foundation C.I.C
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

-- Stream for notifying that a room has become un-partial-stated.
CREATE TABLE un_partial_stated_room_stream(
    -- Position in the stream
    stream_id BIGINT PRIMARY KEY NOT NULL,

    -- Which instance wrote this entry.
    instance_name TEXT NOT NULL,

    -- Which room has been un-partial-stated.
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE
);

-- We want an index here because of the foreign key constraint:
-- upon deleting a room, the database needs to be able to check here.
-- This index is not unique because we can join a room multiple times in a server's lifetime,
-- so the same room could be un-partial-stated multiple times!
CREATE INDEX un_partial_stated_room_stream_room_id ON un_partial_stated_room_stream (room_id);
