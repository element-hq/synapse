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
/* Copyright 2022 The Matrix.org Foundation C.I.C
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

-- next_token_id is a foreign key reference, so previously required a table scan
-- when a row in the referenced table was deleted.
-- As it was self-referential and cascaded deletes, this led to O(t*n) time to
-- delete a row, where t: number of rows in the table and n: number of rows in
-- the ancestral 'chain' of access tokens.
--
-- This index is partial since we only require it for rows which reference
-- another.
-- Performance was tested to be the same regardless of whether the index was
-- full or partial, but a partial index can be smaller.
CREATE INDEX refresh_tokens_next_token_id
    ON refresh_tokens(next_token_id)
    WHERE next_token_id IS NOT NULL;
