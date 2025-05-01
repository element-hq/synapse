--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Remove the old unreferenced state group deletion background update if it exists
DELETE FROM background_updates WHERE update_name = 'delete_unreferenced_state_groups_bg_update';
