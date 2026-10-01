--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2026 Element Creations Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.

-- Represents a stream of when media is quarantined and unquarantined. Note that it's possible for duplicate rows to
-- exist in this table. When the background update which backfills this table is running, it orders quarantined media
-- by `media_id`. This table is also populated when an admin newly quarantines a piece of media. If the admin happens
-- to newly quarantine media that hasn't yet been processed by the background update, both the admin's API call and the
-- background update will add a row to this table. If the background update has already progressed past the media's ID,
-- then only the admin's API call will add a row to this table.
--
-- Note also that this table might not be inserted with all possible cases of media being quarantined. For example, if
-- media is quarantined by hash upon upload or URL preview, it might not show up here. See https://github.com/element-hq/synapse/issues/19672
-- for more details.
--
-- Overall, this table is very much intended to be *best effort* for media that was quarantined before the table existed.
CREATE TABLE quarantined_media_changes (
    -- Position in the quarantined media stream
    stream_id INTEGER NOT NULL PRIMARY KEY,

    -- Name of the worker sending this (makes us compatible with multiple writers)
    instance_name TEXT NOT NULL,

    -- Media origin. NULL if local media.
    -- We store the origin and media_id as media is scoped to the origin and are uniquely identified by (origin, media_id).
    origin TEXT NULL,

    -- Media ID at the origin.
    media_id TEXT NOT NULL,

    -- True if quarantined at this position, false otherwise.
    quarantined BOOLEAN NOT NULL
);

-- Start the background update to populate existing quarantined media in the table. See update handler for more details.
INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
    (9305, 'flag_existing_quarantined_media', '{}');
