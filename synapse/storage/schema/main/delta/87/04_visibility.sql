--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2024 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

CREATE TABLE IF NOT EXISTS history_visibility_ranges (
    room_id TEXT NOT NULL,
    visibility TEXT NOT NULL,
    start_range BIGINT NOT NULL,
    end_range BIGINT
);

CREATE INDEX history_visibility_ranges_idx ON history_visibility_ranges(room_id, start_range, end_range DESC);
CREATE UNIQUE INDEX history_visibility_ranges_uniq_idx ON history_visibility_ranges(room_id, start_range);

-- CREATE EXTENSION IF NOT EXISTS btree_gist;
-- CREATE INDEX history_visibility_ranges_idx_gist ON history_visibility_ranges USING gist(room_id, int8range(start_range, end_range, '[)]'));
