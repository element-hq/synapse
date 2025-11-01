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

-- Store when delayed events have either been sent, cancelled, or not sent due to an error (MSC4140)
ALTER TABLE delayed_events ADD COLUMN finalised_error bytea;
ALTER TABLE delayed_events ADD COLUMN finalised_event_id TEXT;
ALTER TABLE delayed_events ADD COLUMN finalised_ts BIGINT;

CREATE INDEX delayed_events_finalised_ts ON delayed_events (finalised_ts);
