--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- The `flag_existing_quarantined_media` background update (added in 94/03) originally
-- shipped with a broken remote media query that skipped some already-quarantined remote
-- media. Now that it's fixed, re-run the update from scratch so any media missed on the
-- first run gets flagged.
--
-- We delete any existing row first: on servers where the update already completed the row
-- was removed, and on servers where it's still pending/mid-run this clears the stale
-- progress so the re-insert below starts cleanly (and avoids a primary key collision).
DELETE FROM background_updates WHERE update_name = 'flag_existing_quarantined_media';

INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9405, 'flag_existing_quarantined_media', '{}');
