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


CREATE TABLE IF NOT EXISTS e2e_device_keys_json (
    user_id TEXT NOT NULL, -- The user these keys are for.
    device_id TEXT NOT NULL, -- Which of the user's devices these keys are for.
    ts_added_ms BIGINT NOT NULL, -- When the keys were uploaded.
    key_json TEXT NOT NULL, -- The keys for the device as a JSON blob.
    CONSTRAINT e2e_device_keys_json_uniqueness UNIQUE (user_id, device_id)
);


CREATE TABLE IF NOT EXISTS e2e_one_time_keys_json (
    user_id TEXT NOT NULL, -- The user this one-time key is for.
    device_id TEXT NOT NULL, -- The device this one-time key is for.
    algorithm TEXT NOT NULL, -- Which algorithm this one-time key is for.
    key_id TEXT NOT NULL, -- An id for suppressing duplicate uploads.
    ts_added_ms BIGINT NOT NULL, -- When this key was uploaded.
    key_json TEXT NOT NULL, -- The key as a JSON blob.
    CONSTRAINT e2e_one_time_keys_json_uniqueness UNIQUE (user_id, device_id, algorithm, key_id)
);
