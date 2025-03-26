#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#


"""
This migration adds triggers that track inserts/deletes from the events table
in order to track long-term statistics about the events that we see.

Triggers cannot be expressed in .sql files, so we have to use a separate file.
"""

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    # Create the `event_stats` table to store these statistics.
    cur.execute(
        """
        CREATE TABLE event_stats (
            unencrypted_message_count INTEGER NOT NULL DEFAULT 0
            e2ee_event_count INTEGER NOT NULL DEFAULT 0
            total_event_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # Insert initial values into the table.
    cur.execute(
        """
        INSERT INTO event_type_count (
            unencrypted_messages_count,
            e2ee_events_count,
            total_count
        ) VALUES (0, 0, 0);
        """
    )

    # Each time an event is inserted into the `events` table, update the stats.
    if isinstance(database_engine, Sqlite3Engine):
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS event_stats_increment_counts
            BEFORE INSERT ON events
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
