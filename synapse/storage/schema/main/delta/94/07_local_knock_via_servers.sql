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

-- Records the remote server that fulfilled a local user's knock on a remote
-- room (i.e. the server that answered our /send_knock request), so that we can
-- route a subsequent rescission of the knock through the same server, and
-- so that we know which server to trust for out-of-band retractions of the
-- knock (MSC4233).
CREATE TABLE local_knock_via_servers (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    -- The server the knock was fulfilled through.
    via_server TEXT NOT NULL,
    -- The event ID of the knock membership event.
    knock_event_id TEXT NOT NULL,
    CONSTRAINT local_knock_via_servers_uniqueness UNIQUE (user_id, room_id)
);
