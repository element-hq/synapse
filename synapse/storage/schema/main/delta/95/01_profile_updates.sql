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

-- Track updates to profile fields for MSC4429 legacy /sync.
CREATE TABLE profile_updates (
  stream_id BIGINT NOT NULL PRIMARY KEY,
  instance_name TEXT NOT NULL,

  -- The full user ID
  user_id TEXT NOT NULL,

  -- Profile action that has happened, see ProfileUpdateAction enum.
  action TEXT NOT NULL,

  -- Profile field name that has been updated,
  -- see https://spec.matrix.org/unstable/client-server-api/#profiles
  -- This is only required if "action" is "update"
  field_name TEXT NULL,

  -- Unix timestamp. Used to determine when to cull rows (to prevent the table
  -- from growing indefinitely).
  inserted_ts BIGINT NOT NULL
);

CREATE INDEX profile_updates_by_user ON profile_updates (user_id, stream_id);
CREATE INDEX profile_updates_by_field ON profile_updates (field_name, stream_id);
CREATE INDEX profile_updates_inserted_ts ON profile_updates (inserted_ts);

-- Track which local users should receive each profile update.
CREATE TABLE profile_updates_per_user (
  stream_id BIGINT NOT NULL REFERENCES profile_updates (stream_id),

  -- The full user ID of the local user that should receive the profile update.
  user_id TEXT NOT NULL,

  -- Unix timestamp. Used to determine when to cull rows (to prevent the table
  -- from growing indefinitely).
  inserted_ts BIGINT NOT NULL
);

CREATE INDEX profile_updates_per_user_by_user_stream ON profile_updates_per_user (user_id, stream_id);
CREATE INDEX profile_updates_per_user_inserted_ts ON profile_updates_per_user (inserted_ts);
