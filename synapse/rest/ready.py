#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
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
from typing import TYPE_CHECKING

from twisted.web.server import Request

from synapse.api.errors import UnrecognizedRequestError
from synapse.http.server import DirectServeJsonResource
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReadyResource(DirectServeJsonResource):
    """A resource which reports whether this process is ready to serve traffic.

    This endpoint reflects whether Synapse can handle traffic, rather than if it
    is "up" (which /health covers). It is intended for use as a readiness probe
    that removes an instance from load-balancer rotation, not as a liveness
    probe that restarts it.

    Note: `SynapseRequest._should_log_request` ensures that requests to
    `/ready` do not get logged at INFO.
    """

    isLeaf = True

    def __init__(self, hs: "HomeServer"):
        super().__init__(clock=hs.get_clock())
        self._hs = hs
        self._store = hs.get_datastores().main
        self._is_worker = hs.config.worker.worker_app is not None
        self._replication_handler = hs.get_replication_command_handler()

    async def _async_render_GET(self, request: Request) -> tuple[int, JsonDict]:
        # Prevent path traversal by ensuring the request path is exactly /ready.
        if request.path != b"/ready":
            raise UnrecognizedRequestError(code=404)

        db_ok = await self._check_db()

        # A worker isn't ready until it can reach the main process (whether via
        # direct TCP replication or Redis pub/sub). The main process itself
        # isn't gated on this: it having zero attached workers/Redis clients
        # right now doesn't make it unhealthy.
        replication_ok = (
            self._replication_handler.connected() if self._is_worker else True
        )

        startup_ok = self._hs.is_synapse_started()

        checks = {
            "db": db_ok,
            "replication": replication_ok,
            "startup_complete": startup_ok,
        }
        return (200 if all(checks.values()) else 503, checks)

    async def _check_db(self) -> bool:
        try:
            await self._store.db_pool.execute("ready_check", "SELECT 1")
            return True
        except Exception:
            logger.warning("Readiness check: database unreachable", exc_info=True)
            return False
