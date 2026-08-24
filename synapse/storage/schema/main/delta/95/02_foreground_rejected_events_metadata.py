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
from typing import cast

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import make_event_from_dict
from synapse.storage._base import db_to_json
from synapse.storage.database import DatabasePool, LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine
from synapse.types import JsonDict

logger = logging.getLogger(__name__)


def run_create(txn: LoggingTransaction, database_engine: BaseDatabaseEngine) -> None:
    """
    This migration forces the `rejected_events_metadata` background
    update scheduled by `59/09rejected_events_metadata.sql` to be completed
    in the foreground if it is still outstanding.
    """

    # Clear the background update whilst also checking it
    txn.execute(
        """
        DELETE FROM background_updates
        WHERE update_name = 'rejected_events_metadata'
        RETURNING 1
        """
    )
    row = txn.fetchone()
    if row is None:
        # The background update has completed, so there is nothing to do.
        return

    logger.warning(
        "`rejected_events_metadata` has not completed in background; running in foreground!"
    )

    last_event_id = ""

    def get_rejected_events(
        txn: LoggingTransaction,
    ) -> list[tuple[str, str, JsonDict, bool, bool]]:
        # Fetch rejected event json, their room version and whether we have
        # inserted them into the state_events or auth_events tables.
        #
        # Note we can assume that events that don't have a corresponding
        # room version are V1 rooms.
        sql = """
            SELECT DISTINCT
                event_id,
                COALESCE(room_version, '1'),
                json,
                state_events.event_id IS NOT NULL,
                event_auth.event_id IS NOT NULL
            FROM rejections
            INNER JOIN event_json USING (event_id)
            LEFT JOIN rooms USING (room_id)
            LEFT JOIN state_events USING (event_id)
            LEFT JOIN event_auth USING (event_id)
            WHERE event_id > ?
            ORDER BY event_id
            LIMIT ?
        """

        txn.execute(
            sql,
            (
                last_event_id,
                # Hardcode a batch size, unlike the background update
                2000,
            ),
        )

        return cast(
            list[tuple[str, str, JsonDict, bool, bool]],
            [(row[0], row[1], db_to_json(row[2]), row[3], row[4]) for row in txn],
        )

    while True:
        results = get_rejected_events(txn)

        if not results:
            return

        state_events = []
        auth_events = []
        for event_id, room_version, event_json, has_state, has_event_auth in results:
            last_event_id = event_id

            if has_state and has_event_auth:
                continue

            room_version_obj = KNOWN_ROOM_VERSIONS.get(room_version)
            if not room_version_obj:
                # We no longer support this room version, so we just ignore the
                # events entirely.
                logger.info(
                    "Ignoring event with unknown room version %r: %r",
                    room_version,
                    event_id,
                )
                continue

            event = make_event_from_dict(event_json, room_version_obj)

            if not event.is_state():
                continue

            if not has_state:
                state_events.append(
                    (event.event_id, event.room_id, event.type, event.state_key)
                )

            if not has_event_auth:
                # Old, dodgy, events may have duplicate auth events, which we
                # need to deduplicate as we have a unique constraint.
                for auth_id in set(event.auth_event_ids()):
                    auth_events.append((event.event_id, event.room_id, auth_id))

        if state_events:
            DatabasePool.simple_insert_many_txn(
                txn,
                table="state_events",
                keys=("event_id", "room_id", "type", "state_key"),
                values=state_events,
            )

        if auth_events:
            DatabasePool.simple_insert_many_txn(
                txn,
                table="event_auth",
                keys=("event_id", "room_id", "auth_id"),
                values=auth_events,
            )
