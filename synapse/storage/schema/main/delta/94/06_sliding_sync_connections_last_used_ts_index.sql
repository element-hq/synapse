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


-- Add an index on `sliding_sync_connections(last_used_ts)` so that finding and
-- deleting expired connections (in `delete_old_sliding_sync_connections`) does
-- not require a sequential scan of the table.
--
-- This is a partial index as we only ever query for rows with a non-NULL
-- `last_used_ts`.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9406, 'sliding_sync_connections_last_used_ts_idx', '{}');
