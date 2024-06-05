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

CREATE TABLE future_groups (
    user_localpart TEXT NOT NULL,
    group_id TEXT NOT NULL,
    PRIMARY KEY (user_localpart, group_id)
);

CREATE TABLE futures (
    future_id INTEGER PRIMARY KEY, -- An alias of rowid in SQLite
    user_localpart TEXT NOT NULL,
    group_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state_key TEXT,
    origin_server_ts BIGINT,
    content bytea NOT NULL,
    FOREIGN KEY (user_localpart, group_id)
        REFERENCES future_groups (user_localpart, group_id) ON DELETE CASCADE
);
-- TODO: Consider a trigger/constraint to disallow adding an action future to an empty group

CREATE INDEX future_group_idx ON futures (user_localpart, group_id);
CREATE INDEX room_state_event_idx ON futures (room_id, event_type, state_key) WHERE state_key IS NOT NULL;

CREATE TABLE future_timeouts (
    future_id INTEGER PRIMARY KEY
        REFERENCES futures (future_id) ON DELETE CASCADE,
    timeout BIGINT NOT NULL,
    timestamp BIGINT NOT NULL
);

CREATE TABLE future_tokens (
    token TEXT PRIMARY KEY,
    future_id INTEGER NOT NULL
        REFERENCES futures (future_id) ON DELETE CASCADE,
    is_send BOOLEAN NOT NULL DEFAULT FALSE,
    is_cancel BOOLEAN NOT NULL DEFAULT FALSE,
    is_refresh BOOLEAN NOT NULL DEFAULT FALSE,
    CHECK (
        + CAST(is_send AS INTEGER)
        + CAST(is_cancel AS INTEGER)
        + CAST(is_refresh AS INTEGER)
        = 1),
    UNIQUE (future_id, is_send, is_cancel, is_refresh)
);
-- TODO: Consider a trigger/constraint to disallow refresh tokens for futures without a timeout
