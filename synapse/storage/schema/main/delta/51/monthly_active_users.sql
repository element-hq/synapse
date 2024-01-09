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
/* Copyright 2018 New Vector Ltd
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

-- a table of monthly active users, for use where blocking based on mau limits
CREATE TABLE monthly_active_users (
    user_id TEXT NOT NULL,
    -- Last time we saw the user. Not guaranteed to be accurate due to rate limiting
    -- on updates, Granularity of updates governed by
    -- synapse.storage.monthly_active_users.LAST_SEEN_GRANULARITY
    -- Measured in ms since epoch.
    timestamp BIGINT NOT NULL
);

CREATE UNIQUE INDEX monthly_active_users_users ON monthly_active_users(user_id);
CREATE INDEX monthly_active_users_time_stamp ON monthly_active_users(timestamp);
