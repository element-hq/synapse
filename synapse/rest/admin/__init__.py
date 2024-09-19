#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020, 2021 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Optional, Tuple

from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.handlers.pagination import PURGE_HISTORY_ACTION_NAME
from synapse.http.server import HttpServer, JsonResource
from synapse.http.servlet import RestServlet, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import admin_patterns, assert_requester_is_admin
from synapse.rest.admin.background_updates import (
    BackgroundUpdateEnabledRestServlet,
    BackgroundUpdateRestServlet,
    BackgroundUpdateStartJobRestServlet,
)
from synapse.rest.admin.devices import (
    DeleteDevicesRestServlet,
    DeviceRestServlet,
    DevicesRestServlet,
)
from synapse.rest.admin.event_reports import (
    EventReportDetailRestServlet,
    EventReportsRestServlet,
)
from synapse.rest.admin.experimental_features import ExperimentalFeaturesRestServlet
from synapse.rest.admin.federation import (
    DestinationMembershipRestServlet,
    DestinationResetConnectionRestServlet,
    DestinationRestServlet,
    ListDestinationsRestServlet,
)
from synapse.rest.admin.media import ListMediaInRoom, register_servlets_for_media_repo
from synapse.rest.admin.registration_tokens import (
    ListRegistrationTokensRestServlet,
    NewRegistrationTokenRestServlet,
    RegistrationTokenRestServlet,
)
from synapse.rest.admin.rooms import (
    BlockRoomRestServlet,
    DeleteRoomStatusByDeleteIdRestServlet,
    DeleteRoomStatusByRoomIdRestServlet,
    ForwardExtremitiesRestServlet,
    JoinRoomAliasServlet,
    ListRoomRestServlet,
    MakeRoomAdminRestServlet,
    RoomEventContextServlet,
    RoomMembersRestServlet,
    RoomMessagesRestServlet,
    RoomRestServlet,
    RoomRestV2Servlet,
    RoomStateRestServlet,
    RoomTimestampToEventRestServlet,
)
from synapse.rest.admin.server_notice_servlet import SendServerNoticeServlet
from synapse.rest.admin.statistics import (
    LargestRoomsStatistics,
    UserMediaStatisticsRestServlet,
)
from synapse.rest.admin.username_available import UsernameAvailableRestServlet
from synapse.rest.admin.users import (
    AccountDataRestServlet,
    AccountValidityRenewServlet,
    DeactivateAccountRestServlet,
    PushersRestServlet,
    RateLimitRestServlet,
    RedactUser,
    RedactUserStatus,
    ResetPasswordRestServlet,
    SearchUsersRestServlet,
    ShadowBanRestServlet,
    SuspendAccountRestServlet,
    UserAdminServlet,
    UserByExternalId,
    UserByThreePid,
    UserMembershipRestServlet,
    UserRegisterServlet,
    UserReplaceMasterCrossSigningKeyRestServlet,
    UserRestServletV2,
    UsersRestServletV2,
    UsersRestServletV3,
    UserTokenRestServlet,
    WhoisRestServlet,
)
from synapse.types import JsonDict, RoomStreamToken, TaskStatus
from synapse.util import SYNAPSE_VERSION

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class VersionServlet(RestServlet):
    PATTERNS = admin_patterns("/server_version$")

    def __init__(self, hs: "HomeServer"):
        self.res = {"server_version": SYNAPSE_VERSION}

    def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        return HTTPStatus.OK, self.res


class PurgeHistoryRestServlet(RestServlet):
    PATTERNS = admin_patterns(
        "/purge_history/(?P<room_id>[^/]*)(/(?P<event_id>[^/]*))?$"
    )

    def __init__(self, hs: "HomeServer"):
        self.pagination_handler = hs.get_pagination_handler()
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, room_id: str, event_id: Optional[str]
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        body = parse_json_object_from_request(request, allow_empty_body=True)

        delete_local_events = bool(body.get("delete_local_events", False))

        # establish the topological ordering we should keep events from. The
        # user can provide an event_id in the URL or the request body, or can
        # provide a timestamp in the request body.
        if event_id is None:
            event_id = body.get("purge_up_to_event_id")

        if event_id is not None:
            event = await self.store.get_event(event_id)

            if event.room_id != room_id:
                raise SynapseError(HTTPStatus.BAD_REQUEST, "Event is for wrong room.")

            # RoomStreamToken expects [int] not Optional[int]
            assert event.internal_metadata.stream_ordering is not None
            room_token = RoomStreamToken(
                topological=event.depth, stream=event.internal_metadata.stream_ordering
            )
            token = await room_token.to_string(self.store)

            logger.info("[purge] purging up to token %s (event_id %s)", token, event_id)
        elif "purge_up_to_ts" in body:
            ts = body["purge_up_to_ts"]
            if type(ts) is not int:  # noqa: E721
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "purge_up_to_ts must be an int",
                    errcode=Codes.BAD_JSON,
                )

            stream_ordering = await self.store.find_first_stream_ordering_after_ts(ts)

            r = await self.store.get_room_event_before_stream_ordering(
                room_id, stream_ordering
            )
            if not r:
                logger.warning(
                    "[purge] purging events not possible: No event found "
                    "(received_ts %i => stream_ordering %i)",
                    ts,
                    stream_ordering,
                )
                raise SynapseError(
                    HTTPStatus.NOT_FOUND,
                    "there is no event to be purged",
                    errcode=Codes.NOT_FOUND,
                )
            (stream, topo, _event_id) = r
            token = "t%d-%d" % (topo, stream)
            logger.info(
                "[purge] purging up to token %s (received_ts %i => "
                "stream_ordering %i)",
                token,
                ts,
                stream_ordering,
            )
        else:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "must specify purge_up_to_event_id or purge_up_to_ts",
                errcode=Codes.BAD_JSON,
            )

        purge_id = await self.pagination_handler.start_purge_history(
            room_id, token, delete_local_events=delete_local_events
        )

        return HTTPStatus.OK, {"purge_id": purge_id}


class PurgeHistoryStatusRestServlet(RestServlet):
    PATTERNS = admin_patterns("/purge_history_status/(?P<purge_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.pagination_handler = hs.get_pagination_handler()
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, purge_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        purge_task = await self.pagination_handler.get_delete_task(purge_id)
        if purge_task is None or purge_task.action != PURGE_HISTORY_ACTION_NAME:
            raise NotFoundError("purge id '%s' not found" % purge_id)

        result: JsonDict = {
            "status": (
                purge_task.status
                if purge_task.status == TaskStatus.COMPLETE
                or purge_task.status == TaskStatus.FAILED
                else "active"
            ),
        }
        if purge_task.error:
            result["error"] = purge_task.error

        return HTTPStatus.OK, result


########################################################################################
#
# please don't add more servlets here: this file is already long and unwieldy. Put
# them in separate files within the 'admin' package.
#
########################################################################################


class AdminRestResource(JsonResource):
    """The REST resource which gets mounted at /_synapse/admin"""

    def __init__(self, hs: "HomeServer"):
        JsonResource.__init__(self, hs, canonical_json=False)
        register_servlets(hs, self)


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    """
    Register all the admin servlets.
    """
    # Admin servlets aren't registered on workers.
    if hs.config.worker.worker_app is not None:
        return

    register_servlets_for_client_rest_resource(hs, http_server)
    BlockRoomRestServlet(hs).register(http_server)
    ListRoomRestServlet(hs).register(http_server)
    RoomStateRestServlet(hs).register(http_server)
    RoomRestServlet(hs).register(http_server)
    RoomRestV2Servlet(hs).register(http_server)
    RoomMembersRestServlet(hs).register(http_server)
    DeleteRoomStatusByDeleteIdRestServlet(hs).register(http_server)
    DeleteRoomStatusByRoomIdRestServlet(hs).register(http_server)
    JoinRoomAliasServlet(hs).register(http_server)
    VersionServlet(hs).register(http_server)
    if not hs.config.experimental.msc3861.enabled:
        UserAdminServlet(hs).register(http_server)
    UserMembershipRestServlet(hs).register(http_server)
    if not hs.config.experimental.msc3861.enabled:
        UserTokenRestServlet(hs).register(http_server)
    UserRestServletV2(hs).register(http_server)
    UsersRestServletV2(hs).register(http_server)
    UsersRestServletV3(hs).register(http_server)
    UserMediaStatisticsRestServlet(hs).register(http_server)
    LargestRoomsStatistics(hs).register(http_server)
    EventReportDetailRestServlet(hs).register(http_server)
    EventReportsRestServlet(hs).register(http_server)
    AccountDataRestServlet(hs).register(http_server)
    PushersRestServlet(hs).register(http_server)
    MakeRoomAdminRestServlet(hs).register(http_server)
    ShadowBanRestServlet(hs).register(http_server)
    ForwardExtremitiesRestServlet(hs).register(http_server)
    RoomEventContextServlet(hs).register(http_server)
    RateLimitRestServlet(hs).register(http_server)
    UsernameAvailableRestServlet(hs).register(http_server)
    if not hs.config.experimental.msc3861.enabled:
        ListRegistrationTokensRestServlet(hs).register(http_server)
        NewRegistrationTokenRestServlet(hs).register(http_server)
        RegistrationTokenRestServlet(hs).register(http_server)
    DestinationMembershipRestServlet(hs).register(http_server)
    DestinationResetConnectionRestServlet(hs).register(http_server)
    DestinationRestServlet(hs).register(http_server)
    ListDestinationsRestServlet(hs).register(http_server)
    RoomMessagesRestServlet(hs).register(http_server)
    RoomTimestampToEventRestServlet(hs).register(http_server)
    UserReplaceMasterCrossSigningKeyRestServlet(hs).register(http_server)
    UserByExternalId(hs).register(http_server)
    UserByThreePid(hs).register(http_server)
    RedactUser(hs).register(http_server)
    RedactUserStatus(hs).register(http_server)

    DeviceRestServlet(hs).register(http_server)
    DevicesRestServlet(hs).register(http_server)
    DeleteDevicesRestServlet(hs).register(http_server)
    SendServerNoticeServlet(hs).register(http_server)
    BackgroundUpdateEnabledRestServlet(hs).register(http_server)
    BackgroundUpdateRestServlet(hs).register(http_server)
    BackgroundUpdateStartJobRestServlet(hs).register(http_server)
    ExperimentalFeaturesRestServlet(hs).register(http_server)
    if hs.config.experimental.msc3823_account_suspension:
        SuspendAccountRestServlet(hs).register(http_server)


def register_servlets_for_client_rest_resource(
    hs: "HomeServer", http_server: HttpServer
) -> None:
    """Register only the servlets which need to be exposed on /_matrix/client/xxx"""
    WhoisRestServlet(hs).register(http_server)
    PurgeHistoryStatusRestServlet(hs).register(http_server)
    PurgeHistoryRestServlet(hs).register(http_server)
    # The following resources can only be run on the main process.
    if hs.config.worker.worker_app is None:
        DeactivateAccountRestServlet(hs).register(http_server)
        if not hs.config.experimental.msc3861.enabled:
            ResetPasswordRestServlet(hs).register(http_server)
    SearchUsersRestServlet(hs).register(http_server)
    if not hs.config.experimental.msc3861.enabled:
        UserRegisterServlet(hs).register(http_server)
        AccountValidityRenewServlet(hs).register(http_server)

    # Load the media repo ones if we're using them. Otherwise load the servlets which
    # don't need a media repo (typically readonly admin APIs).
    if hs.config.media.can_load_media_repo:
        register_servlets_for_media_repo(hs, http_server)
    else:
        ListMediaInRoom(hs).register(http_server)

    # don't add more things here: new servlets should only be exposed on
    # /_synapse/admin so should not go here. Instead register them in AdminRestResource.
