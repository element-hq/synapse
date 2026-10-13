#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine, Sqlite3Engine

logger = logging.getLogger(__name__)


def run_create(txn: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    """
    This migration forces the `event_edges_drop_invalid_rows` background update
    scheduled by `71/01rebuild_event_edges.sql.postgres` to be completed
    in the foreground if it is still outstanding.
    """

    if isinstance(database_engine, Sqlite3Engine):
        # SQLite already did everything synchronously in
        # `71/01rebuild_event_edges.sql.sqlite`
        # So there's nothing to do
        return

    # Clear the background update whilst also checking it
    txn.execute(
        """
        DELETE FROM background_updates
        WHERE update_name = 'event_edges_drop_invalid_rows'
        RETURNING 1
        """
    )
    if txn.fetchone() is None:
        # The background update has completed, so there is nothing to do.
        return

    logger.warning(
        "`event_edges_drop_invalid_rows` has not completed in background; running in foreground!"
    )

    # now delete any that:
    #   - have is_state=TRUE, or
    #   - do not correspond to a row in `events`
    txn.execute(
        """
        DELETE FROM event_edges
        WHERE event_id IN (
           SELECT ee.event_id
           FROM event_edges ee
             LEFT JOIN events ev USING (event_id)
           WHERE (is_state OR ev.event_id IS NULL)
        )
        """,
    )
    logger.info("Deleted %i legacy state edges from `event_edges` table", txn.rowcount)

    logger.info("Enabling foreign key")
    txn.execute(
        """
        ALTER TABLE event_edges
            VALIDATE CONSTRAINT event_edges_event_id_fkey
        """
    )
