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


CREATE TABLE IF NOT EXISTS state_epoch (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    state_epoch BIGINT NOT NULL,
    updated_ts BIGINT NOT NULL,
    CHECK (Lock='X')
);

INSERT INTO state_epoch (state_epoch, updated_ts) VALUES (0, 0);

CREATE TABLE IF NOT EXISTS state_groups_pending_deletion (
    state_group BIGINT NOT NULL,
    state_epoch BIGINT NOT NULL,
    PRIMARY KEY (state_group, state_epoch)
);

CREATE INDEX state_groups_pending_deletion_epoch ON state_groups_pending_deletion(state_epoch);
