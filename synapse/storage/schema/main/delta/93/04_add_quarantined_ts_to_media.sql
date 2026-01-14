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
ALTER TABLE local_media_repository ADD COLUMN quarantined_ts BIGINT;
ALTER TABLE remote_media_cache ADD COLUMN quarantined_ts BIGINT;

UPDATE local_media_repository SET quarantined_ts = 0 WHERE quarantined_by IS NOT NULL;
UPDATE remote_media_cache SET quarantined_ts = 0 WHERE quarantined_by IS NOT NULL;

-- Note: We *probably* should have an index on quarantined_ts, but we're going
-- to try to defer that to a future migration after seeing the performance impact.
