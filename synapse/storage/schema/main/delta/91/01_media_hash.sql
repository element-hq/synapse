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

-- Add a column `sha256` to `local_media_repository` table.
ALTER TABLE local_media_repository ADD COLUMN sha256 TEXT;
ALTER TABLE remote_media_cache ADD COLUMN sha256 TEXT;

-- Add a cache
CREATE INDEX local_media_repository_sha256 ON local_media_repository (sha256, media_id);
CREATE INDEX remote_media_cache_sha256 ON remote_media_cache (sha256, media_id);
