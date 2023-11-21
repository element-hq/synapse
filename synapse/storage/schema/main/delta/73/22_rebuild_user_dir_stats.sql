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

INSERT INTO background_updates (ordering, update_name, progress_json, depends_on) VALUES
  -- Set up user directory staging tables.
  (7322, 'populate_user_directory_createtables', '{}', NULL),
  -- Run through each room and update the user directory according to who is in it.
  (7322, 'populate_user_directory_process_rooms', '{}', 'populate_user_directory_createtables'),
  -- Insert all users into the user directory, if search_all_users is on.
  (7322, 'populate_user_directory_process_users', '{}', 'populate_user_directory_process_rooms'),
  -- Clean up user directory staging tables.
  (7322, 'populate_user_directory_cleanup', '{}', 'populate_user_directory_process_users'),
  -- Rebuild the room_stats_current and room_stats_state tables.
  (7322, 'populate_stats_process_rooms', '{}', NULL),
  -- Update the user_stats_current table.
  (7322, 'populate_stats_process_users', '{}', NULL)
ON CONFLICT (update_name) DO NOTHING;
