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

import logging
from typing import List, Optional, Tuple, cast

from synapse.api.errors import SynapseError
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.main import CacheInvalidationWorkerStore
from synapse.storage.databases.main.event_federation import EventFederationWorkerStore

logger = logging.getLogger(__name__)


class EventForwardExtremitiesStore(
    EventFederationWorkerStore,
    CacheInvalidationWorkerStore,
):
    async def delete_forward_extremities_for_room(self, room_id: str) -> int:
        """Delete any extra forward extremities for a room.

        Invalidates the "get_latest_event_ids_in_room" cache if any forward
        extremities were deleted.

        Returns count deleted.
        """

        def delete_forward_extremities_for_room_txn(txn: LoggingTransaction) -> int:
            # First we need to get the event_id to not delete
            sql = """
                SELECT event_id FROM event_forward_extremities
                INNER JOIN events USING (room_id, event_id)
                WHERE room_id = ?
                ORDER BY stream_ordering DESC
                LIMIT 1
            """
            txn.execute(sql, (room_id,))
            rows = txn.fetchall()
            try:
                event_id = rows[0][0]
                logger.debug(
                    "Found event_id %s as the forward extremity to keep for room %s",
                    event_id,
                    room_id,
                )
            except KeyError:
                msg = "No forward extremity event found for room %s" % room_id
                logger.warning(msg)
                raise SynapseError(400, msg)

            # Now delete the extra forward extremities
            sql = """
                DELETE FROM event_forward_extremities
                WHERE event_id != ? AND room_id = ?
            """

            txn.execute(sql, (event_id, room_id))

            deleted_count = txn.rowcount
            logger.info(
                "Deleted %s extra forward extremities for room %s",
                deleted_count,
                room_id,
            )

            if deleted_count > 0:
                # Invalidate the cache
                self._invalidate_cache_and_stream(
                    txn,
                    self.get_latest_event_ids_in_room,
                    (room_id,),
                )

            return deleted_count

        return await self.db_pool.runInteraction(
            "delete_forward_extremities_for_room",
            delete_forward_extremities_for_room_txn,
        )

    async def get_forward_extremities_for_room(
        self, room_id: str
    ) -> List[Tuple[str, int, int, Optional[int]]]:
        """
        Get list of forward extremities for a room.

        Returns:
            A list of tuples of event_id, state_group, depth, and received_ts.
        """

        def get_forward_extremities_for_room_txn(
            txn: LoggingTransaction,
        ) -> List[Tuple[str, int, int, Optional[int]]]:
            sql = """
                SELECT event_id, state_group, depth, received_ts
                FROM event_forward_extremities
                INNER JOIN event_to_state_groups USING (event_id)
                INNER JOIN events USING (room_id, event_id)
                WHERE room_id = ?
            """

            txn.execute(sql, (room_id,))
            return cast(List[Tuple[str, int, int, Optional[int]]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_forward_extremities_for_room",
            get_forward_extremities_for_room_txn,
        )
