#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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


"""
This migration adds triggers to the partial_state_events tables to enforce uniqueness

Triggers cannot be expressed in .sql files, so we have to use a separate file.
"""
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    # complain if the room_id in partial_state_events doesn't match
    # that in `events`. We already have a fk constraint which ensures that the event
    # exists in `events`, so all we have to do is raise if there is a row with a
    # matching stream_ordering but not a matching room_id.
    if isinstance(database_engine, Sqlite3Engine):
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS partial_state_events_bad_room_id
            BEFORE INSERT ON partial_state_events
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'Incorrect room_id in partial_state_events')
                WHERE EXISTS (
                    SELECT 1 FROM events
                    WHERE events.event_id = NEW.event_id
                       AND events.room_id != NEW.room_id
                );
            END;
            """
        )
    elif isinstance(database_engine, PostgresEngine):
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION check_partial_state_events() RETURNS trigger AS $BODY$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM events
                    WHERE events.event_id = NEW.event_id
                       AND events.room_id != NEW.room_id
                ) THEN
                    RAISE EXCEPTION 'Incorrect room_id in partial_state_events';
                END IF;
                RETURN NEW;
            END;
            $BODY$ LANGUAGE plpgsql;
            """
        )

        cur.execute(
            """
            CREATE TRIGGER check_partial_state_events BEFORE INSERT OR UPDATE ON partial_state_events
            FOR EACH ROW
            EXECUTE PROCEDURE check_partial_state_events()
            """
        )
    else:
        raise NotImplementedError("Unknown database engine")
