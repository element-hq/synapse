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

-- Tracks sticky events.
-- Excludes 'policy_server_spammy' events, outliers, rejected events.
--
-- Skipping the insertion of these types of 'invalid' events is useful for performance reasons because
-- they would fill up the table yet we wouldn't show them to clients anyway.
--
-- Since syncing clients can't (easily) 'skip over' sticky events (due to being in-order, reliably delivered),
-- tracking loads of invalid events in the table could make it expensive for servers to retrieve the sticky events that are actually valid.
--
-- For instance, someone spamming 1000s of rejected or 'policy_server_spammy' events could clog up this table in a way that means we either
-- have to deliver empty payloads to syncing clients, or consider substantially more than 100 events in order to gather a 100-sized batch to send down.
--
-- May contain sticky events that have expired since being inserted,
-- although they will be periodically cleaned up in the background.
CREATE TABLE sticky_events (
  -- Position in the sticky events stream
  stream_id INTEGER NOT NULL PRIMARY KEY,

  -- Name of the worker sending this. (This makes the stream compatible with multiple writers.)
  instance_name TEXT NOT NULL,

  -- The event ID of the sticky event itself.
  event_id TEXT NOT NULL,

  -- The room ID that the sticky event is in.
  -- Denormalised for performance. (Safe as it's an immutable property of the event.)
  room_id TEXT NOT NULL,

  -- The stream_ordering of the event.
  -- Denormalised for performance since we will want to sort these by stream_ordering
  -- when fetching them. (Safe as it's an immutable property of the event.)
  event_stream_ordering INTEGER NOT NULL UNIQUE,

  -- Sender of the sticky event.
  -- Denormalised for performance so we can query only for sticky events originating
  -- from our homeserver. (Safe as it's an immutable property of the event.)
  sender TEXT NOT NULL,

  -- When the sticky event expires, in milliseconds since the Unix epoch.
  expires_at BIGINT NOT NULL
);

-- For pulling out sticky events by room at send time, obeying stream ordering range limits.
CREATE INDEX sticky_events_room_idx ON sticky_events (room_id, event_stream_ordering);

-- A optional integer for combining sticky events with delayed events. Used at send time.
ALTER TABLE delayed_events ADD COLUMN sticky_duration_ms BIGINT;
