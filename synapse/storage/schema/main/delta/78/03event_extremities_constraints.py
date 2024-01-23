#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
This migration adds foreign key constraint to `event_forward_extremities` table.
"""
from synapse.storage.background_updates import (
    ForeignKeyConstraint,
    run_validate_constraint_and_delete_rows_schema_delta,
)
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine

FORWARD_EXTREMITIES_TABLE_SCHEMA = """
    CREATE TABLE event_forward_extremities2(
        event_id TEXT NOT NULL,
        room_id TEXT NOT NULL,
        UNIQUE (event_id, room_id),
        CONSTRAINT event_forward_extremities_event_id FOREIGN KEY (event_id) REFERENCES events (event_id) DEFERRABLE INITIALLY DEFERRED
    )
"""


def run_create(cur: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    # We mark this as a deferred constraint, as the previous version of Synapse
    # inserted the event into the forward extremities *before* the events table.
    # By marking as deferred we ensure that downgrading to the previous version
    # will continue to work.
    run_validate_constraint_and_delete_rows_schema_delta(
        cur,
        ordering=7803,
        update_name="event_forward_extremities_event_id_foreign_key_constraint_update",
        table="event_forward_extremities",
        constraint_name="event_forward_extremities_event_id",
        constraint=ForeignKeyConstraint(
            "events", [("event_id", "event_id")], deferred=True
        ),
        sqlite_table_name="event_forward_extremities2",
        sqlite_table_schema=FORWARD_EXTREMITIES_TABLE_SCHEMA,
    )

    # We can't add a similar constraint to `event_backward_extremities` as the
    # events in there don't exist in the `events` table and `event_edges`
    # doesn't have a unique constraint on `prev_event_id` (so we can't make a
    # foreign key point to it).
