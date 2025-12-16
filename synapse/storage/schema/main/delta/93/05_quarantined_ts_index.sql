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

INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
  -- we should start at ordering 9305, but there's higher conflict if we steal 9306's ordering, so
  -- we'll steal 9304's ordering instead. 9304 is where the applicable columns were added.
  (9304, 'local_media_repository_quarantined_ts_idx', '{}'),
  (9305, 'remote_media_cache_quarantined_ts_idx', '{}');
