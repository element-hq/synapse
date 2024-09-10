--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2024 Patrick Cloke
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Custom profile fields.
CREATE TABLE profile_fields (
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT NOT NULL
);

CREATE UNIQUE INDEX profile_fields_user_name ON profile_fields (user_id, name);
