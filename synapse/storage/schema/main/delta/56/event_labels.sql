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
/* Copyright 2019 The Matrix.org Foundation C.I.C.
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

-- room_id and topological_ordering are denormalised from the events table in order to
-- make the index work.
CREATE TABLE IF NOT EXISTS event_labels (
    event_id TEXT,
    label TEXT,
    room_id TEXT NOT NULL,
    topological_ordering BIGINT NOT NULL,
    PRIMARY KEY(event_id, label)
);


-- This index enables an event pagination looking for a particular label to index the
-- event_labels table first, which is much quicker than scanning the events table and then
-- filtering by label, if the label is rarely used relative to the size of the room.
CREATE INDEX event_labels_room_id_label_idx ON event_labels(room_id, label, topological_ordering);
