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

-- Add a background update to update the `sliding_sync_membership_snapshots` ->
-- `forgotten` column to be in sync with the `room_memberships` table.
--
-- For any room that someone has forgotten and subsequently re-joined or had any new
-- membership on, we need to go and update the column to match the `room_memberships`
-- table as it has fallen out of sync.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  (8802, 'sliding_sync_membership_snapshots_fix_forgotten_column_bg_update', '{}');
