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


-- Create the `event_stats` table to store these statistics.
CREATE TABLE event_stats (
    total_event_count INTEGER NOT NULL DEFAULT 0,
    unencrypted_message_count INTEGER NOT NULL DEFAULT 0,
    e2ee_event_count INTEGER NOT NULL DEFAULT 0
);

-- Insert initial values into the table.
INSERT INTO event_stats (
    total_event_count,
    unencrypted_message_count,
    e2ee_event_count
) VALUES (0, 0, 0);

-- Add a background update to populate the `event_stats` table with the current counts
-- from the `events` table and add triggers to keep this count up-to-date.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9101, 'event_stats_populate_counts_bg_update', '{}');

