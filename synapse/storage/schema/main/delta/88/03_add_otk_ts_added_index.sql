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


-- Add an index on (user_id, device_id, algorithm, ts_added_ms) on e2e_one_time_keys_json, so that OTKs can
-- efficiently be issued in the same order they were uploaded.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (8803, 'add_otk_ts_added_index', '{}');
