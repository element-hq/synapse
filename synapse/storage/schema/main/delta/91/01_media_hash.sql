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

-- Store the SHA256 content hash of media files.
ALTER TABLE local_media_repository ADD COLUMN sha256 TEXT;
ALTER TABLE remote_media_cache ADD COLUMN sha256 TEXT;

-- Add a background updates to handle creating the new index.
--
-- Note that the ordering of the update is not following the usual scheme. This
-- is because when upgrading from Synapse 1.127, this index is fairly important
-- to have up quickly, so that it doesn't tank performance, which is why it is
-- scheduled before other background updates in the 1.127 -> 1.128 upgrade
INSERT INTO
  background_updates (ordering, update_name, progress_json)
VALUES
  (8890, 'local_media_repository_sha256_idx', '{}'),
  (8891, 'remote_media_cache_sha256_idx', '{}');
