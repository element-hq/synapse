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

CREATE SEQUENCE IF NOT EXISTS sticky_events_seq;

CREATE TABLE IF NOT EXISTS sticky_events(
  id BIGINT PRIMARY KEY DEFAULT nextval('sticky_events_seq'),
  room_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  expires_at BIGINT NOT NULL,
  soft_failed BOOLEAN NOT NULL
);

-- for pulling out soft failed events by room
CREATE INDEX IF NOT EXISTS sticky_events_room_idx ON sticky_events(room_id, soft_failed);
