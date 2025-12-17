--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 Element Creations, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Add a timestamp for when the sliding sync connection position was last used,
-- only updated with a small granularity.
--
-- This should be NOT NULL, but we need to consider existing rows. In future we
-- may want to either backfill this or delete all rows with a NULL value (and
-- then make it NOT NULL).
ALTER TABLE sliding_sync_connections ADD COLUMN last_used_ts BIGINT;

-- Note: We don't add an index on this column to allow HOT updates on PostgreSQL
-- to reduce the cost of the updates to the column. c.f.
-- https://www.postgresql.org/docs/current/storage-hot.html
--
-- We do query this column directly to find expired connections, but we expect
-- that to be an infrequent operation and a sequential scan should be fine.
