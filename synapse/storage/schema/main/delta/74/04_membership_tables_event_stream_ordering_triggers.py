#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 Beeper
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
This migration adds triggers to the room membership tables to enforce consistency.
Triggers cannot be expressed in .sql files, so we have to use a separate file.
"""

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    # Complain if the `event_stream_ordering` in membership tables doesn't match
    # the `stream_ordering` row with the same `event_id` in `events`.
    if isinstance(database_engine, Sqlite3Engine):
        for table in (
            "current_state_events",
            "local_current_membership",
            "room_memberships",
        ):
            cur.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_bad_event_stream_ordering
                BEFORE INSERT ON {table}
                FOR EACH ROW
                BEGIN
                    SELECT RAISE(ABORT, 'Incorrect event_stream_ordering in {table}')
                    WHERE EXISTS (
                        SELECT 1 FROM events
                        WHERE events.event_id = NEW.event_id
                           AND events.stream_ordering != NEW.event_stream_ordering
                    );
                END;
                """
            )
    elif isinstance(database_engine, PostgresEngine):
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION check_event_stream_ordering() RETURNS trigger AS $BODY$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM events
                    WHERE events.event_id = NEW.event_id
                       AND events.stream_ordering != NEW.event_stream_ordering
                ) THEN
                    RAISE EXCEPTION 'Incorrect event_stream_ordering';
                END IF;
                RETURN NEW;
            END;
            $BODY$ LANGUAGE plpgsql;
            """
        )

        for table in (
            "current_state_events",
            "local_current_membership",
            "room_memberships",
        ):
            cur.execute(
                f"""
                CREATE TRIGGER check_event_stream_ordering BEFORE INSERT OR UPDATE ON {table}
                FOR EACH ROW
                EXECUTE PROCEDURE check_event_stream_ordering()
                """
            )
    else:
        raise NotImplementedError("Unknown database engine")
