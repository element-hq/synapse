--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2023 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.
--
-- Originally licensed under the Apache License, Version 2.0:
-- <http://www.apache.org/licenses/LICENSE-2.0>.
--
-- [This file includes modifications made by New Vector Limited]
--
--
/* Copyright 2015, 2016 OpenMarket Ltd
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

CREATE TABLE IF NOT EXISTS schema_version(
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    version INTEGER NOT NULL,
    upgraded BOOL NOT NULL,  -- Whether we reached this version from an upgrade or an initial schema.
    CHECK (Lock='X')
);

CREATE TABLE IF NOT EXISTS schema_compat_version(
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    -- The SCHEMA_VERSION of the oldest synapse this database can be used with
    compat_version INTEGER NOT NULL,
    CHECK (Lock='X')
);

CREATE TABLE IF NOT EXISTS applied_schema_deltas(
    version INTEGER NOT NULL,
    file TEXT NOT NULL,
    UNIQUE(version, file)
);

-- a list of schema files we have loaded on behalf of dynamic modules
CREATE TABLE IF NOT EXISTS applied_module_schemas(
    module_name TEXT NOT NULL,
    file TEXT NOT NULL,
    UNIQUE(module_name, file)
);
