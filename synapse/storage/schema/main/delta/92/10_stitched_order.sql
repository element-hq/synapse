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

ALTER TABLE events ADD COLUMN stitched_ordering BIGINT;
CREATE UNIQUE INDEX events_stitched_order ON events(room_id, stitched_ordering); -- TODO make this concurrent

--CREATE TABLE stitched_ordering_gaps (
--    room_id TEXT NOT NULL,
--    following_event_id TEXT NOT NULL,
--    missing_event_id TEXT NOT NULL,
--    UNIQUE (room_id, following_event_id, missing_event_id)
--);
--CREATE INDEX stitched_ordering_gaps_missing_events ON stitched_ordering_gaps(room_id, missing_event_id);

-- Gaps in the stitched ordering are equivalent to a group of backward extremities that appear at
-- the same point in the stitched ordering.
--
-- Rather than explicitly tracking where in the stitched ordering a given gap appears, we record the
-- event id of the event that comes *before* the gap in the stitched ordering. Doing so means that:
--
--   1. There is only one table that has a `stitched_ordering` column, making it easier to figure out
--      how to insert a batch of events between existing events (and making the UNIQUE constraint effective).
--
--   2. We don't need to allocate space in the stitched ordering for gaps; in particular we can assign an order
--      to gaps *after* we have persisted the events. (We could probably work around this by double-spacing inserted
--      events? but still, it's a nice property)
--
-- Note that this assumes that we never need to insert an event *before* a gap (or if we did,
-- we'd have to update this table).
ALTER TABLE event_backward_extremities ADD COLUMN before_gap_event_id TEXT REFERENCES events (event_id);
