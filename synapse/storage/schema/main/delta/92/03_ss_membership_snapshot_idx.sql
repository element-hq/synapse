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

-- So we can fetch all rooms for a given user sorted by stream order
DROP INDEX IF EXISTS sliding_sync_membership_snapshots_user_id;
CREATE INDEX IF NOT EXISTS sliding_sync_membership_snapshots_user_id ON sliding_sync_membership_snapshots(user_id, event_stream_ordering);
