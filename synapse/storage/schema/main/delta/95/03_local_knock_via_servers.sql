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
--
-- At most one row per local user and knocked room: a later knock on the same
-- room overwrites the previous one. Rows are not deleted when the knock ends,
-- so the table grows with the number of rooms local users have ever knocked
-- on; MSC4233 only requires the record to survive as long as the knock is the
-- user's membership.
CREATE TABLE local_knock_via_servers (
    user_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    -- The server the knock was fulfilled through.
    via_server TEXT NOT NULL,
    -- The event ID of the knock membership event.
    knock_event_id TEXT NOT NULL,
    PRIMARY KEY (user_id, room_id)
);
