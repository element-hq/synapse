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


-- Table to track active sliding sync connections.
--
-- A new connection will be created for every sliding sync request without a
-- `since` token for a given `conn_id` for a device.#
--
-- Once a new connection is created and used we delete all other connections for
-- the `conn_id`.
CREATE TABLE sliding_sync_connections(
    connection_key $%AUTO_INCREMENT_PRIMARY_KEY%$,
    user_id TEXT NOT NULL,
    -- Generally the device ID, but may be something else for e.g. puppeted accounts.
    effective_device_id TEXT NOT NULL,
    conn_id TEXT NOT NULL,
    created_ts BIGINT NOT NULL
);

CREATE INDEX sliding_sync_connections_idx ON sliding_sync_connections(user_id, effective_device_id, conn_id);
CREATE INDEX sliding_sync_connections_ts_idx ON sliding_sync_connections(created_ts);

-- We track per-connection state by associating changes to the state with
-- connection positions. This ensures that we correctly track state even if we
-- see retries of requests.
--
-- If the client starts a "new" connection (by not specifying a since token),
-- we'll clear out the other connections (to ensure that we don't end up with
-- lots of connection keys).
CREATE TABLE sliding_sync_connection_positions(
    connection_position $%AUTO_INCREMENT_PRIMARY_KEY%$,
    connection_key BIGINT NOT NULL REFERENCES sliding_sync_connections(connection_key) ON DELETE CASCADE,
    created_ts BIGINT NOT NULL
);

CREATE INDEX sliding_sync_connection_positions_key ON sliding_sync_connection_positions(connection_key);
CREATE INDEX sliding_sync_connection_positions_ts_idx ON sliding_sync_connection_positions(created_ts);


-- To save space we deduplicate the `required_state` json by assigning IDs to
-- different values.
CREATE TABLE sliding_sync_connection_required_state(
    required_state_id $%AUTO_INCREMENT_PRIMARY_KEY%$,
    connection_key BIGINT NOT NULL REFERENCES sliding_sync_connections(connection_key) ON DELETE CASCADE,
    required_state TEXT NOT NULL  -- We store this as a json list of event type / state key tuples.
);

CREATE INDEX sliding_sync_connection_required_state_conn_pos ON sliding_sync_connection_required_state(connection_key);


-- Stores the room configs we have seen for rooms in a connection.
CREATE TABLE sliding_sync_connection_room_configs(
    connection_position BIGINT NOT NULL REFERENCES sliding_sync_connection_positions(connection_position) ON DELETE CASCADE,
    room_id TEXT NOT NULL,
    timeline_limit BIGINT NOT NULL,
    required_state_id BIGINT NOT NULL REFERENCES sliding_sync_connection_required_state(required_state_id)
);

CREATE UNIQUE INDEX sliding_sync_connection_room_configs_idx ON sliding_sync_connection_room_configs(connection_position, room_id);

-- Stores what data we have sent for given streams down given connections.
CREATE TABLE sliding_sync_connection_streams(
    connection_position BIGINT NOT NULL REFERENCES sliding_sync_connection_positions(connection_position) ON DELETE CASCADE,
    stream TEXT NOT NULL,  -- e.g. "events" or "receipts"
    room_id TEXT NOT NULL,
    room_status TEXT NOT NULL,  -- "live" or "previously", i.e. the `HaveSentRoomFlag` value
    last_token TEXT  -- For "previously" the token for the stream we have sent up to.
);

CREATE UNIQUE INDEX sliding_sync_connection_streams_idx ON sliding_sync_connection_streams(connection_position, room_id, stream);
