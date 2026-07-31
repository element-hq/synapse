--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd.
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Track updates to profile fields.
-- For MSC4429 legacy /sync and others.
-- See https://github.com/element-hq/synapse/issues/19981 for potential future directions of this table.
CREATE TABLE IF NOT EXISTS profile_updates (
  stream_id BIGINT NOT NULL PRIMARY KEY,
  instance_name TEXT NOT NULL,

  -- The full user ID
  user_id TEXT NOT NULL,

  -- Profile action that has happened, see ProfileUpdateAction enum.
  action TEXT NOT NULL,

  -- JSON array of the profile field names that have been
  -- added, updated or removed in this update.
  -- See https://spec.matrix.org/unstable/client-server-api/#profiles
  -- This is only present if `action` is `update`.
  --
  -- We support multiple field updates at once because it is easy to foresee features
  -- involving multiple fields (where getting the illusion of a torn write might be harmful),
  -- as well as synchronisation over federation being likely to lead to multiple field changes
  -- at once.
  affected_fields JSONB NULL,

  -- Unix timestamp (milliseconds) for debugging purposes
  inserted_ts BIGINT NOT NULL
);

CREATE INDEX IF NOT EXISTS profile_updates_by_user ON profile_updates (user_id, stream_id);

-- We aren't creating a GIN index on `affected_fields` at this time because we don't expect
-- field names to be very selective and therefore an index might not be that useful.

-- Track which local users should receive each profile update.
CREATE TABLE IF NOT EXISTS profile_updates_per_user (
  -- Stream ID reference to `profile_updates`
  stream_id BIGINT NOT NULL REFERENCES profile_updates (stream_id),

  -- The full user ID of the local user that should receive the profile update.
  user_id TEXT NOT NULL,

  -- Unix timestamp (milliseconds). Used to determine when to prune rows (to prevent the table
  -- from growing indefinitely).
  inserted_ts BIGINT NOT NULL,

  PRIMARY KEY (user_id, stream_id)
);
