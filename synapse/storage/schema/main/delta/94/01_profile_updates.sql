--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 New Vector, Ltd
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

  user_id TEXT NOT NULL,
  field_name TEXT NOT NULL,

  CONSTRAINT profile_updates_fk_users
    FOREIGN KEY (user_id)
    REFERENCES users(name) ON DELETE CASCADE
);

CREATE INDEX profile_updates_by_user ON profile_updates (user_id, stream_id);
CREATE INDEX profile_updates_by_stream ON profile_updates (stream_id);
CREATE INDEX profile_updates_by_field ON profile_updates (field_name, stream_id);
