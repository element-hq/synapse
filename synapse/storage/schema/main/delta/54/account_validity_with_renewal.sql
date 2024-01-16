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
/* Copyright 2019 New Vector Ltd
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

-- We previously changed the schema for this table without renaming the file, which means
-- that some databases might still be using the old schema. This ensures Synapse uses the
-- right schema for the table.
DROP TABLE IF EXISTS account_validity;

-- Track what users are in public rooms.
CREATE TABLE IF NOT EXISTS account_validity (
    user_id TEXT PRIMARY KEY,
    expiration_ts_ms BIGINT NOT NULL,
    email_sent BOOLEAN NOT NULL,
    renewal_token TEXT
);

CREATE INDEX account_validity_email_sent_idx ON account_validity(email_sent, expiration_ts_ms)
CREATE UNIQUE INDEX account_validity_renewal_string_idx ON account_validity(renewal_token)
