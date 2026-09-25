--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Background update that restores chronological ordering for events whose
-- topological_ordering collapsed onto MAX_DEPTH (or above it, in room versions
-- without strict canonical JSON).
INSERT INTO background_updates (ordering, update_name, progress_json, depends_on) VALUES
  (9409, 'fixup_max_depth_tie_ordering', '{}', 'fixup_max_depth_cap');
