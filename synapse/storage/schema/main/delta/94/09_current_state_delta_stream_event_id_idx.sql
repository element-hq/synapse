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


-- Add an index on `current_state_delta_stream(event_id)` so that the deltas
-- of the state events in a sync window can be looked up by event, even when
-- the rows are stamped before the window (rows are stamped with the minimum
-- stream ordering of their persist batch, see `_update_current_state_txn`).
--
-- This is a partial index as rows with a NULL event_id (state deletions) are
-- never looked up by event.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9409, 'current_state_delta_stream_event_id_index', '{}');
