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

-- See the `StateDeletionDataStore` for details of these tables.

-- We add state groups to this table when we want to later delete them. The
-- `insertion_ts` column indicates when the state group was proposed for
-- deletion (rather than when it should be deleted).
CREATE TABLE IF NOT EXISTS state_groups_pending_deletion (
    sequence_number $%AUTO_INCREMENT_PRIMARY_KEY%$,
    state_group BIGINT NOT NULL,
    insertion_ts BIGINT NOT NULL
);

CREATE UNIQUE INDEX state_groups_pending_deletion_state_group ON state_groups_pending_deletion(state_group);
CREATE INDEX state_groups_pending_deletion_insertion_ts ON state_groups_pending_deletion(insertion_ts);


-- Holds the state groups the worker is currently persisting.
--
-- The `sequence_number` column of the `state_groups_pending_deletion` table
-- *must* be updated whenever a state group may have become referenced.
CREATE TABLE IF NOT EXISTS state_groups_persisting (
    state_group BIGINT NOT NULL,
    instance_name TEXT NOT NULL,
    PRIMARY KEY (state_group, instance_name)
);

CREATE INDEX state_groups_persisting_instance_name ON state_groups_persisting(instance_name);
