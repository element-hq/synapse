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
/* Copyright 2021 The Matrix.org Foundation C.I.C
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

-- Holds MSC2918 refresh tokens
CREATE TABLE refresh_tokens (
  id BIGINT PRIMARY KEY,
  user_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  token TEXT NOT NULL,
  -- When consumed, a new refresh token is generated, which is tracked by
  -- this foreign key
  next_token_id BIGINT REFERENCES refresh_tokens (id) ON DELETE CASCADE,
  UNIQUE(token)
);

-- Add a reference to the refresh token generated alongside each access token
ALTER TABLE "access_tokens"
  ADD COLUMN refresh_token_id BIGINT REFERENCES refresh_tokens (id) ON DELETE CASCADE;

-- Add a flag whether the token was already used or not
ALTER TABLE "access_tokens"
  ADD COLUMN used BOOLEAN;
