#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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
#
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import SynapseError, Codes
from synapse.api.ratelimiting import Ratelimiter
from synapse.types import (
    Requester,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReportsHandler:
    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()

        # Ratelimiter for management of existing delayed events,
        # keyed by the requesting user ID.
        self._reports_ratelimiter = Ratelimiter(
            store=self._store,
            clock=self._clock,
            cfg=hs.config.ratelimiting.rc_reports,
        )

    async def report_user(self, requester: Requester, target_user_id: str, reason: str) -> None:
        await self._check_limits(requester)

        if len(reason) > 1000:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Reason must be less than 1000 characters",
                Codes.BAD_JSON,
            )

        if not self._hs.is_mine_id(target_user_id):
            return  # hide that they're not ours/that we can't do anything about them

        user = await self._store.get_user_by_id(target_user_id)
        if user is None:
            return  # hide that they don't exist

        await self._store.add_user_report(
            target_user_id=target_user_id,
            user_id=requester.user.to_string(),
            reason=reason,
            received_ts=self._clock.time_msec(),
        )

    async def _check_limits(self, requester: Requester) -> None:
        await self._reports_ratelimiter.ratelimit(
            requester,
            requester.user.to_string(),
        )
