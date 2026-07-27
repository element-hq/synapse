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


-- Add an index on `sliding_sync_connection_lazy_members(connection_position)`
-- so that deleting from `sliding_sync_connection_positions` is efficient. This
-- is needed because `connection_position` has an `ON DELETE CASCADE` foreign key
-- constraint, and without this index Postgres has to sequentially scan the whole
-- table for each deleted position.
--
-- This is a partial index as we only ever need to find rows with a non-NULL
-- `connection_position`.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9407, 'sliding_sync_connection_lazy_members_conn_pos_idx', '{}');
