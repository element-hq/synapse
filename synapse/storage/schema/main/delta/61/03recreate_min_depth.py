#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
This migration handles the process of changing the type of `room_depth.min_depth` to
a BIGINT.
"""

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    if not isinstance(database_engine, PostgresEngine):
        # this only applies to postgres - sqlite does not distinguish between big and
        # little ints.
        return

    # First add a new column to contain the bigger min_depth
    cur.execute("ALTER TABLE room_depth ADD COLUMN min_depth2 BIGINT")

    # Create a trigger which will keep it populated.
    cur.execute(
        """
        CREATE OR REPLACE FUNCTION populate_min_depth2() RETURNS trigger AS $BODY$
            BEGIN
                new.min_depth2 := new.min_depth;
                RETURN NEW;
            END;
        $BODY$ LANGUAGE plpgsql
        """
    )

    cur.execute(
        """
        CREATE TRIGGER populate_min_depth2_trigger BEFORE INSERT OR UPDATE ON room_depth
        FOR EACH ROW
        EXECUTE PROCEDURE populate_min_depth2()
        """
    )

    # Start a bg process to populate it for old rooms
    cur.execute(
        """
       INSERT INTO background_updates (ordering, update_name, progress_json) VALUES
            (6103, 'populate_room_depth_min_depth2', '{}')
       """
    )

    # and another to switch them over once it completes.
    cur.execute(
        """
        INSERT INTO background_updates (ordering, update_name, progress_json, depends_on) VALUES
            (6103, 'replace_room_depth_min_depth', '{}', 'populate_room_depth2')
        """
    )
