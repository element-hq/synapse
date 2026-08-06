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

import json
import logging

from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine

logger = logging.getLogger(__name__)


def run_create(txn: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    """
    This migration forces the `events_populate_state_key_rejections` background
    update scheduled by `72/03bg_populate_events_columns.py` to be completed
    in the foreground if it is still outstanding.
    """

    # Clear the background update whilst also checking it
    txn.execute(
        """
        DELETE FROM background_updates
        WHERE update_name = 'events_populate_state_key_rejections'
        RETURNING progress_json
        """
    )
    row = txn.fetchone()
    if row is None:
        # The background update has completed, so there is nothing to do.
        return

    progress = json.loads(row[0])
    min_stream_ordering_exclusive = progress["min_stream_ordering_exclusive"]
    max_stream_ordering_inclusive = progress["max_stream_ordering_inclusive"]

    logger.warning(
        "`events_populate_state_key_rejections` has not completed in background; running in foreground!"
    )

    # Backpopulate the `state_key` and `rejection_reason` columns according to the unpopulated range
    # specified in the background update progress dict.
    # For simplicity, do this in one query.
    txn.execute(
        """
        UPDATE events
        SET state_key = (SELECT state_key FROM state_events se WHERE se.event_id = events.event_id),
            rejection_reason = (SELECT reason FROM rejections rej WHERE rej.event_id = events.event_id)
        WHERE ? < stream_ordering AND stream_ordering <= ?
        """,
        (min_stream_ordering_exclusive, max_stream_ordering_inclusive),
    )
    logger.info("Populated new `events` columns for %i rows", txn.rowcount)
