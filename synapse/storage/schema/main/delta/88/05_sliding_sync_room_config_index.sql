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


-- Add an index on sliding_sync_connection_room_configs(required_state_id), so
-- that when we delete entries in `sliding_sync_connection_required_state` it's
-- efficient for Postgres to check they've been deleted from
-- `sliding_sync_connection_room_configs` too
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (8805, 'sliding_sync_connection_room_configs_required_state_id_idx', '{}');
