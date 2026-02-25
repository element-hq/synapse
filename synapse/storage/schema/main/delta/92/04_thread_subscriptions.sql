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

-- Introduce a table for tracking users' subscriptions to threads.
CREATE TABLE thread_subscriptions (
  stream_id INTEGER NOT NULL PRIMARY KEY,
  instance_name TEXT NOT NULL,

  room_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  user_id TEXT NOT NULL,

  subscribed BOOLEAN NOT NULL,
  automatic BOOLEAN NOT NULL,

  CONSTRAINT thread_subscriptions_fk_users
    FOREIGN KEY (user_id)
    REFERENCES users(name),

  CONSTRAINT thread_subscriptions_fk_rooms
    FOREIGN KEY (room_id)
    -- When we delete a room, we should already have deleted all the events in that room
    -- and so there shouldn't be any subscriptions left in that room.
    -- So the `ON DELETE CASCADE` should be optional, but included anyway for good measure.
    REFERENCES rooms(room_id) ON DELETE CASCADE,

  CONSTRAINT thread_subscriptions_fk_events
    FOREIGN KEY (event_id)
    REFERENCES events(event_id) ON DELETE CASCADE,

  -- This order provides a useful index for:
  -- 1. foreign key constraint on (room_id)
  -- 2. foreign key constraint on (room_id, event_id)
  -- 3. finding the user's settings for a specific thread (as well as enforcing uniqueness)
  UNIQUE (room_id, event_id, user_id)
);

-- this provides a useful index for finding a user's own rules,
-- potentially scoped to a single room
CREATE INDEX thread_subscriptions_user_room ON thread_subscriptions (user_id, room_id);

-- this provides a useful way for clients to efficiently find new changes to
-- their subscriptions.
-- (This is necessary to sync subscriptions between multiple devices.)
CREATE INDEX thread_subscriptions_by_user ON thread_subscriptions (user_id, stream_id);

-- this provides a useful index for deleting the subscriptions when the underlying
-- events are removed. This also covers the foreign key constraint on `events`.
CREATE INDEX thread_subscriptions_by_event ON thread_subscriptions (event_id);
