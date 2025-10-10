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

CREATE TABLE finalised_delayed_events (
    delay_id TEXT NOT NULL,
    user_localpart TEXT NOT NULL,
    error bytea,
    event_id TEXT,
    finalised_ts BIGINT NOT NULL,
    PRIMARY KEY (user_localpart, delay_id),
    FOREIGN KEY (user_localpart, delay_id)
        REFERENCES delayed_events (user_localpart, delay_id) ON DELETE CASCADE
);

CREATE INDEX finalised_delayed_events_finalised_ts ON finalised_delayed_events (finalised_ts);
