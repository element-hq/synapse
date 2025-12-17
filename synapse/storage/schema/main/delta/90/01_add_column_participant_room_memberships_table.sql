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

-- Add a column `participant` to `room_memberships` table to track whether a room member has sent
-- a `m.room.message` or `m.room.encrypted` event into a room they are a member of
ALTER TABLE room_memberships ADD COLUMN participant BOOLEAN DEFAULT FALSE;