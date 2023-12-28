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
/* Copyright 2020 The Matrix.org Foundation C.I.C
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

-- This originally was in 58/, but landed after 59/ was created, and so some
-- servers running develop didn't run this delta. Running it again should be
-- safe.
--
-- We first delete any in progress `rejected_events_metadata` background update,
-- to ensure that we don't conflict when trying to insert the new one. (We could
-- alternatively do an ON CONFLICT DO NOTHING, but that syntax isn't supported
-- by older SQLite versions. Plus, this should be a rare case).
DELETE FROM background_updates WHERE update_name = 'rejected_events_metadata';
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  (5828, 'rejected_events_metadata', '{}');
