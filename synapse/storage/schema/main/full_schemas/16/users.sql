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
/* Copyright 2014-2016 OpenMarket Ltd
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
CREATE TABLE IF NOT EXISTS users(
    name TEXT,
    password_hash TEXT,
    creation_ts BIGINT,
    admin SMALLINT DEFAULT 0 NOT NULL,
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS access_tokens(
    id BIGINT PRIMARY KEY,
    user_id TEXT NOT NULL,
    device_id TEXT,
    token TEXT NOT NULL,
    last_used BIGINT,
    UNIQUE(token)
);

CREATE TABLE IF NOT EXISTS user_ips (
    user_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    device_id TEXT,
    ip TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    last_seen BIGINT NOT NULL
);

CREATE INDEX user_ips_user ON user_ips(user_id);
CREATE INDEX user_ips_user_ip ON user_ips(user_id, access_token, ip);
