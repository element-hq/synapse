--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

CREATE TABLE IF NOT EXISTS msc4242_state_dag_forward_extremities(
    -- we always expect the room to exist. If it gets removed, delete fwd extremities.
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    -- it doesn't make sense to reference the same event multiple times, and this uniqueness
    -- index is also used to delete events once they are no longer forward extremities.
    UNIQUE (event_id, room_id)
);
-- When creating events, we want to select all forward extremities for a room which this index helps with.
CREATE INDEX msc4242_state_dag_room ON msc4242_state_dag_forward_extremities(room_id);


CREATE TABLE IF NOT EXISTS msc4242_state_dag_edges(
    -- Deleting the room deletes the state DAG.
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    -- the event IDs being referenced must exist (hence REFERENCES) and we do not want to accidentally delete
    -- the event and create a hole in the state DAG. It is not possible for a state
    -- DAG room to function with an holey DAG, so these events _cannot_ be purged. To purge them, the
    -- entire room would need to be deleted.
    event_id TEXT NOT NULL REFERENCES events(event_id),
    -- one of the `prev_state_events` for this event ID. We must have it since we must have the entire state DAG.
    -- can be NULL for the create event.
    prev_state_event_id TEXT REFERENCES events(event_id)
    -- calculated depth for this event ID. There is some denormalisation here as we're storing the depth
    -- multiple times for each prev_state_event_id.
    -- depth BIGINT NOT NULL
);
CREATE INDEX msc4242_state_dag_edges_by_room ON msc4242_state_dag_edges(room_id);
CREATE UNIQUE INDEX msc4242_state_dag_edges_key ON msc4242_state_dag_edges(room_id, event_id, prev_state_event_id);
