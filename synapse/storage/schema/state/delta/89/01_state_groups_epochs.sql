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

-- See the `StateEpochDataStore` for details of these tables.

-- Holds the current state epoch
CREATE TABLE IF NOT EXISTS state_epoch (
    Lock CHAR(1) NOT NULL DEFAULT 'X' UNIQUE,  -- Makes sure this table only has one row.
    state_epoch BIGINT NOT NULL,
    updated_ts BIGINT NOT NULL,
    CHECK (Lock='X')
);

-- Insert a row so that we always have one row in the table. This will get
-- updated when Synapse starts.
INSERT INTO state_epoch (state_epoch, updated_ts) VALUES (0, 0);


-- We add state groups to this table when we want to later delete them. The
-- `state_epoch` column indicates when the state group was inserted.
CREATE TABLE IF NOT EXISTS state_groups_pending_deletion (
    state_group BIGINT NOT NULL,
    state_epoch BIGINT NOT NULL,
    PRIMARY KEY (state_group, state_epoch)
);

CREATE INDEX state_groups_pending_deletion_epoch ON state_groups_pending_deletion(state_epoch);


-- Holds the state groups the worker is currently persisting.
CREATE TABLE IF NOT EXISTS state_groups_persisting (
    state_group BIGINT NOT NULL,
    instance_name TEXT NOT NULL,
    PRIMARY KEY (state_group, instance_name)
);

CREATE INDEX state_groups_persisting_instance_name ON state_groups_persisting(instance_name);
