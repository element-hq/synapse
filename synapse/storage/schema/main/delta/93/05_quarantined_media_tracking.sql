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

CREATE TABLE quarantined_media_changes (
    -- Position in the quarantined media stream
    stream_id INTEGER NOT NULL PRIMARY KEY,

    -- Name of the worker sending this (makes us compatible with multiple writers)
    instance_name TEXT NOT NULL,

    -- Media origin. NULL if local media.
    origin TEXT NULL,

    -- Media ID at the origin.
    media_id TEXT NOT NULL,

    -- True if quarantined at this position, false otherwise.
    quarantined BOOLEAN NOT NULL
);
