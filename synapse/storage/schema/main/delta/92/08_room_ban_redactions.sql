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

CREATE TABLE room_ban_redactions(
    room_id text NOT NULL,
    user_id text NOT NULL,
    redacting_event_id text NOT NULL,
    redact_end_ordering bigint DEFAULT NULL, -- stream ordering after which redactions are not applied
    CONSTRAINT room_ban_redaction_uniqueness UNIQUE (room_id, user_id)
);

