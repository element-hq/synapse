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


CREATE TABLE IF NOT EXISTS new_server_keys_json (
    server_name TEXT NOT NULL, -- Server name.
    key_id TEXT NOT NULL, -- Requested key id.
    from_server TEXT NOT NULL, -- Which server the keys were fetched from.
    ts_added_ms BIGINT NOT NULL, -- When the keys were fetched
    ts_valid_until_ms BIGINT NOT NULL, -- When this version of the keys exipires.
    key_json bytea NOT NULL, -- JSON certificate for the remote server.
    CONSTRAINT server_keys_json_uniqueness UNIQUE (server_name, key_id, from_server)
);

INSERT INTO new_server_keys_json
    SELECT server_name, key_id, from_server,ts_added_ms, ts_valid_until_ms, key_json FROM server_keys_json ;

DROP TABLE server_keys_json;

ALTER TABLE new_server_keys_json RENAME TO server_keys_json;
