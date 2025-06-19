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

CREATE TABLE user_reports (
    id BIGINT NOT NULL PRIMARY KEY,
    received_ts BIGINT NOT NULL,
    target_user_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX user_reports_target_user_id ON user_reports(target_user_id); -- for lookups
CREATE INDEX user_reports_user_id ON user_reports(user_id); -- for lookups
