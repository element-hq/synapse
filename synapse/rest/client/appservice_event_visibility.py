# Copyright 2024 The Matrix.org Foundation C.I.C
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import (
    Codes,
    SynapseError,
)
from synapse.http.server import HttpServer
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.visibility import filter_events_for_client

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class AppserviceEventVisibilityrestServlet(RestServlet):
    PATTERNS = client_patterns(
        "/net.tadzik/appservice/(?P<appservice_id>[^/]*)/can_user_see_event/(?P<room_id>[^/]*)/(?P<user_id>[^/]*)/(?P<event_id>[^/]*)$",
        unstable=True,
        releases=(),
    )

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()

    async def on_GET(
        self, request: SynapseRequest, appservice_id: str, room_id: str, user_id: str, event_id: str
    ) -> Tuple[int, bool]:
        logger.info('on_GET')
        requester = await self.auth.get_user_by_req(request)

        if not requester.app_service:
            raise SynapseError(
                HTTPStatus.FORBIDDEN,
                "Only application services can use the /can_user_see_event endpoint",
                Codes.FORBIDDEN,
            )
        elif requester.app_service.id != appservice_id:
            raise SynapseError(
                HTTPStatus.FORBIDDEN,
                "Mismatching application service ID in path",
                Codes.FORBIDDEN,
            )

        event = await self.store.get_event(event_id, check_room_id=room_id)
        filtered = await filter_events_for_client(self._storage_controllers, user_id, [event])

        return HTTPStatus.OK, bool(filtered)

def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if hs.config.experimental.msc4185_enabled:
        AppserviceEventVisibilityrestServlet(hs).register(http_server)
