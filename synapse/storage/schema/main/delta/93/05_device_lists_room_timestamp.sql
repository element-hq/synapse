--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 Element Creations, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

ALTER TABLE device_lists_changes_in_room ADD COLUMN inserted_ts BIGINT;

-- Add a background update to add index
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  (9005, 'device_lists_changes_in_room_inserted_ts_idx', '{}');
