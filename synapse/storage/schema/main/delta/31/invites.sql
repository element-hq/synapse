--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2023 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.
--
-- Originally licensed under the Apache License, Version 2.0:
-- <http://www.apache.org/licenses/LICENSE-2.0>.
--
-- [This file includes modifications made by New Vector Limited]
--
--
/* Copyright 2016 OpenMarket Ltd
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */


CREATE TABLE local_invites(
    stream_id BIGINT NOT NULL,
    inviter TEXT NOT NULL,
    invitee TEXT NOT NULL,
    event_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    locally_rejected TEXT,
    replaced_by TEXT
);

-- Insert all invites for local users into new `invites` table
INSERT INTO local_invites SELECT
        stream_ordering as stream_id,
        sender as inviter,
        state_key as invitee,
        event_id,
        room_id,
        NULL as locally_rejected,
        NULL as replaced_by
    FROM events
    NATURAL JOIN current_state_events
    NATURAL JOIN room_memberships
    WHERE membership = 'invite'  AND state_key IN (SELECT name FROM users);

CREATE INDEX local_invites_id ON local_invites(stream_id);
CREATE INDEX local_invites_for_user_idx ON local_invites(invitee, locally_rejected, replaced_by, room_id);
