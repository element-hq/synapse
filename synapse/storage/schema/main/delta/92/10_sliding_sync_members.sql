--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2025 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.


CREATE TABLE sliding_sync_connection_lazy_members (
    connection_key BIGINT NOT NULL REFERENCES sliding_sync_connections(connection_key) ON DELETE CASCADE,
    connection_position BIGINT REFERENCES sliding_sync_connection_positions(connection_position) ON DELETE CASCADE,
    room_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    last_seen_ts BIGINT NOT NULL
);

CREATE UNIQUE INDEX sliding_sync_connection_lazy_members_idx ON sliding_sync_connection_lazy_members (connection_key, room_id, user_id);
CREATE INDEX sliding_sync_connection_lazy_members_pos_idx ON sliding_sync_connection_lazy_members (connection_key, connection_position) WHERE connection_position IS NOT NULL;
