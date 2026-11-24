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

-- Add an index on `lower(name)` to `users`, so that case-insensitive user ID
-- lookups (e.g. when checking whether a username is available) don't need to
-- scan the whole table.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9502, 'users_lower_name', '{}');
