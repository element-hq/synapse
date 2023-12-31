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

-- Stream for notifying that an event has become un-partial-stated.
CREATE TABLE un_partial_stated_event_stream(
    -- Position in the stream
    stream_id BIGINT PRIMARY KEY NOT NULL,

    -- Which instance wrote this entry.
    instance_name TEXT NOT NULL,

    -- Which event has been un-partial-stated.
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,

    -- true iff the `rejected` status of the event changed when it became
    -- un-partial-stated.
    rejection_status_changed BOOLEAN NOT NULL
);

-- We want an index here because of the foreign key constraint:
-- upon deleting an event, the database needs to be able to check here.
CREATE UNIQUE INDEX un_partial_stated_event_stream_room_id ON un_partial_stated_event_stream (event_id);
