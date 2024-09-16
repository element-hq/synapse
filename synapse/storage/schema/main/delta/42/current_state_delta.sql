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
/* Copyright 2017 Vector Creations Ltd
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

-- Tracks what the server thinks is the current state of the room as time goes. It does
-- not track how state progresses from the beginning of the room. So for example, when
-- you remotely join a room, the first rows will just be the state when you joined and
-- progress from there.
CREATE TABLE current_state_delta_stream (
    stream_id BIGINT NOT NULL,
    room_id TEXT NOT NULL,
    type TEXT NOT NULL,
    state_key TEXT NOT NULL,
    event_id TEXT,  -- Is null if the key was removed
    prev_event_id TEXT  -- Is null if the key was added
);

CREATE INDEX current_state_delta_stream_idx ON current_state_delta_stream(stream_id);
