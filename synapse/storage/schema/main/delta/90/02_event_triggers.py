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
            unencrypted_message_count INTEGER NOT NULL DEFAULT 0,
            e2ee_event_count INTEGER NOT NULL DEFAULT 0,
            total_event_count INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # Insert initial values into the table.
    cur.execute(
        """
        INSERT INTO event_stats (
            unencrypted_message_count,
            e2ee_event_count,
            total_event_count
        ) VALUES (0, 0, 0);
        """
    )

    # Each time an event is inserted into the `events` table, update the stats.
    #
    # We're using `AFTER` triggers as we want to count successful inserts/deletes and
    # not the ones that could potentially fail.
    if isinstance(database_engine, Sqlite3Engine):
        cur.execute(
            """
            CREATE TRIGGER events_insert_trigger
            AFTER INSERT ON events
            BEGIN
                -- Always increment total_event_count
                UPDATE event_stats SET total_event_count = total_event_count + 1;

                -- Increment unencrypted_message_count for m.room.message events
                UPDATE event_stats
                SET unencrypted_message_count = unencrypted_message_count + 1
                WHERE NEW.type = 'm.room.message' AND NEW.state_key IS NULL;

                -- Increment e2ee_event_count for m.room.encrypted events
                UPDATE event_stats
                SET e2ee_event_count = e2ee_event_count + 1
                WHERE NEW.type = 'm.room.encrypted' AND NEW.state_key IS NULL;
            END;
            """
        )

        cur.execute(
            """
            CREATE TRIGGER events_delete_trigger
            AFTER DELETE ON events
            BEGIN
                -- Always decrement total_event_count
                UPDATE event_stats SET total_event_count = total_event_count - 1;

                -- Decrement unencrypted_message_count for m.room.message events
                UPDATE event_stats
                SET unencrypted_message_count = unencrypted_message_count - 1
                WHERE OLD.type = 'm.room.message' AND OLD.state_key IS NULL;

                -- Decrement e2ee_event_count for m.room.encrypted events
                UPDATE event_stats
                SET e2ee_event_count = e2ee_event_count - 1
                WHERE OLD.type = 'm.room.encrypted' AND OLD.state_key IS NULL;
            END;
            """
        )
    elif isinstance(database_engine, PostgresEngine):
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION event_stats_increment_counts() RETURNS trigger AS $BODY$
            BEGIN
                IF TG_OP = 'INSERT' THEN
                    -- Always increment total_event_count
                    UPDATE event_stats SET total_event_count = total_event_count + 1;

                    -- Increment unencrypted_message_count for m.room.message events
                    IF NEW.type = 'm.room.message' AND NEW.state_key IS NULL THEN
                        UPDATE event_stats SET unencrypted_message_count = unencrypted_message_count + 1;
                    END IF;

                    -- Increment e2ee_event_count for m.room.encrypted events
                    IF NEW.type = 'm.room.encrypted' AND NEW.state_key IS NULL THEN
                        UPDATE event_stats SET e2ee_event_count = e2ee_event_count + 1;
                    END IF;

                    -- We're not modifying the row being inserted/deleted, so we return it unchanged.
                    RETURN NEW;

                ELSIF TG_OP = 'DELETE' THEN
                    -- Always decrement total_event_count
                    UPDATE event_stats SET total_event_count = total_event_count - 1;

                    -- Decrement unencrypted_message_count for m.room.message events
                    IF OLD.type = 'm.room.message' AND OLD.state_key IS NULL THEN
                        UPDATE event_stats SET unencrypted_message_count = unencrypted_message_count - 1;
                    END IF;

                    -- Decrement e2ee_event_count for m.room.encrypted events
                    IF OLD.type = 'm.room.encrypted' AND OLD.state_key IS NULL THEN
                        UPDATE event_stats SET e2ee_event_count = e2ee_event_count - 1;
                    END IF;

                    -- "The usual idiom in DELETE triggers is to return OLD."
                    -- (https://www.postgresql.org/docs/current/plpgsql-trigger.html)
                    RETURN OLD;
                ELSE
                    RAISE EXCEPTION 'update_event_stats() was run with unexpected operation (%). '
                        'This indicates a trigger misconfiguration as this function should only'
                        'run with INSERT/DELETE operations.', TG_OP;
                END IF;

            END;
            $BODY$ LANGUAGE plpgsql;
            """
        )

        cur.execute(
            """
            CREATE TRIGGER event_stats_increment_counts
            AFTER INSERT OR DELETE ON events
            FOR EACH ROW
            EXECUTE PROCEDURE event_stats_increment_counts()
            """
        )
    else:
        raise NotImplementedError("Unknown database engine")
