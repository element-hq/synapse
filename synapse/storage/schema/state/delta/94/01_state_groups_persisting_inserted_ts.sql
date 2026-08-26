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

-- Record when we marked a state group as being persisted, so that we can clear
-- out rows left behind by a persist that never finished.
--
-- Nullable, as rows written by an instance that predates this column have no
-- timestamp.
ALTER TABLE state_groups_persisting ADD COLUMN inserted_ts BIGINT;
