#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine
from synapse.storage.prepare_database import get_statements

logger = logging.getLogger(__name__)

DROP_INDICES = """
-- We only ever query based on event_id
DROP INDEX IF EXISTS state_events_room_id;
DROP INDEX IF EXISTS state_events_type;
DROP INDEX IF EXISTS state_events_state_key;

-- room_id is indexed elsewhere
DROP INDEX IF EXISTS current_state_events_room_id;
DROP INDEX IF EXISTS current_state_events_state_key;
DROP INDEX IF EXISTS current_state_events_type;

DROP INDEX IF EXISTS transactions_have_ref;

-- (topological_ordering, stream_ordering, room_id) seems like a strange index,
-- and is used incredibly rarely.
DROP INDEX IF EXISTS events_order_topo_stream_room;

-- an equivalent index to this actually gets re-created in delta 41, because it
-- turned out that deleting it wasn't a great plan :/. In any case, let's
-- delete it here, and delta 41 will create a new one with an added UNIQUE
-- constraint
DROP INDEX IF EXISTS event_search_ev_idx;
"""

POSTGRES_DROP_CONSTRAINT = """
ALTER TABLE event_auth DROP CONSTRAINT IF EXISTS event_auth_event_id_auth_id_room_id_key;
"""

SQLITE_DROP_CONSTRAINT = """
DROP INDEX IF EXISTS evauth_edges_id;

CREATE TABLE IF NOT EXISTS event_auth_new(
    event_id TEXT NOT NULL,
    auth_id TEXT NOT NULL,
    room_id TEXT NOT NULL
);

INSERT INTO event_auth_new
    SELECT event_id, auth_id, room_id
    FROM event_auth;

DROP TABLE event_auth;

ALTER TABLE event_auth_new RENAME TO event_auth;

CREATE INDEX evauth_edges_id ON event_auth(event_id);
"""


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    for statement in get_statements(DROP_INDICES.splitlines()):
        cur.execute(statement)

    if isinstance(database_engine, PostgresEngine):
        drop_constraint = POSTGRES_DROP_CONSTRAINT
    else:
        drop_constraint = SQLITE_DROP_CONSTRAINT

    for statement in get_statements(drop_constraint.splitlines()):
        cur.execute(statement)
