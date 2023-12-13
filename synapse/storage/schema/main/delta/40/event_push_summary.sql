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
/* Copyright 2017 OpenMarket Ltd
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

-- Aggregate of notification counts up to `stream_ordering`, including those
-- that may have been deleted out of the main event_push_actions table. This
-- count does not include those that were highlights, as they remain in the
-- event_push_actions table.
CREATE TABLE event_push_summary (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    notif_count BIGINT NOT NULL,
    stream_ordering BIGINT NOT NULL
);

CREATE INDEX event_push_summary_user_rm ON event_push_summary(user_id, room_id);


-- The stream ordering up to which we have aggregated the event_push_actions
-- table into event_push_summary
CREATE TABLE event_push_summary_stream_ordering (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    stream_ordering BIGINT NOT NULL,
    CHECK (Lock='X')
);

INSERT INTO event_push_summary_stream_ordering (stream_ordering) VALUES (0);
