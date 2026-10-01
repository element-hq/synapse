--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.


-- Add an index on `device_lists_changes_in_room(user_id, device_id, room_id,
-- stream_id)` for unconverted rows, so that marking redundant rows as
-- converted (in `mark_redundant_device_lists_pokes`) does not require a scan
-- of the unconverted backlog.
--
-- This is a partial index as we only ever query for unconverted rows.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9408, 'device_lists_changes_in_room_unconverted_idx', '{}');
