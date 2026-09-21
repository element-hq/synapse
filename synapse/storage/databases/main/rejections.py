#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
from typing import Set

from synapse.storage._base import SQLBaseStore

logger = logging.getLogger(__name__)


class RejectionsStore(SQLBaseStore):
    async def get_rejection_reason(self, event_id: str) -> str | None:
        return await self.db_pool.simple_select_one_onecol(
            table="rejections",
            retcol="reason",
            keyvalues={"event_id": event_id},
            allow_none=True,
            desc="get_rejection_reason",
        )

    async def get_rejected_events(self, event_ids: Set[str]) -> Set[str]:
        """Filter the provided event IDs to only return rejected events."""
        rows = await self.db_pool.simple_select_many_batch(
            table="rejections",
            column="event_id",
            iterable=event_ids,
            retcols=("event_id",),
            keyvalues={},
            desc="get_rejected_events",
        )
        return {r[0] for r in rows}
