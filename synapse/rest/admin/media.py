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
from http import HTTPStatus
from typing import TYPE_CHECKING, Optional, Tuple

import attr

from synapse.api.constants import Direction
from synapse.api.errors import Codes, NotFoundError, SynapseError
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_boolean,
    parse_enum,
    parse_integer,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.rest.admin._base import (
    admin_patterns,
    assert_requester_is_admin,
    assert_user_is_admin,
)
from synapse.storage.databases.main.media_repository import MediaSortOrder
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class QuarantineMediaInRoom(RestServlet):
    """Quarantines all media in a room so that no one can download it via
    this server.
    """

    PATTERNS = [
        *admin_patterns("/room/(?P<room_id>[^/]*)/media/quarantine$"),
        # This path kept around for legacy reasons
        *admin_patterns("/quarantine_media/(?P<room_id>[^/]*)$"),
    ]

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)

        logging.info("Quarantining room: %s", room_id)

        # Quarantine all media in this room
        num_quarantined = await self.store.quarantine_media_ids_in_room(
            room_id, requester.user.to_string()
        )

        return HTTPStatus.OK, {"num_quarantined": num_quarantined}


class QuarantineMediaByUser(RestServlet):
    """Quarantines all local media by a given user so that no one can download it via
    this server.
    """

    PATTERNS = admin_patterns("/user/(?P<user_id>[^/]*)/media/quarantine$")

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)

        logging.info("Quarantining media by user: %s", user_id)

        # Quarantine all media this user has uploaded
        num_quarantined = await self.store.quarantine_media_ids_by_user(
            user_id, requester.user.to_string()
        )

        return HTTPStatus.OK, {"num_quarantined": num_quarantined}


class QuarantineMediaByID(RestServlet):
    """Quarantines local or remote media by a given ID so that no one can download
    it via this server.
    """

    PATTERNS = admin_patterns(
        "/media/quarantine/(?P<server_name>[^/]*)/(?P<media_id>[^/]*)$"
    )

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, server_name: str, media_id: str
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        await assert_user_is_admin(self.auth, requester)

        logging.info("Quarantining media by ID: %s/%s", server_name, media_id)

        # Quarantine this media id
        await self.store.quarantine_media_by_id(
            server_name, media_id, requester.user.to_string()
        )

        return HTTPStatus.OK, {}


class UnquarantineMediaByID(RestServlet):
    """Quarantines local or remote media by a given ID so that no one can download
    it via this server.
    """

    PATTERNS = admin_patterns(
        "/media/unquarantine/(?P<server_name>[^/]*)/(?P<media_id>[^/]*)$"
    )

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, server_name: str, media_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        logging.info("Remove from quarantine media by ID: %s/%s", server_name, media_id)

        # Remove from quarantine this media id
        await self.store.quarantine_media_by_id(server_name, media_id, None)

        return HTTPStatus.OK, {}


class ProtectMediaByID(RestServlet):
    """Protect local media from being quarantined."""

    PATTERNS = admin_patterns("/media/protect/(?P<media_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, media_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        logging.info("Protecting local media by ID: %s", media_id)

        # Protect this media id
        await self.store.mark_local_media_as_safe(media_id, safe=True)

        return HTTPStatus.OK, {}


class UnprotectMediaByID(RestServlet):
    """Unprotect local media from being quarantined."""

    PATTERNS = admin_patterns("/media/unprotect/(?P<media_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_POST(
        self, request: SynapseRequest, media_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        logging.info("Unprotecting local media by ID: %s", media_id)

        # Unprotect this media id
        await self.store.mark_local_media_as_safe(media_id, safe=False)

        return HTTPStatus.OK, {}


class ListMediaInRoom(RestServlet):
    """Lists all of the media in a given room."""

    PATTERNS = admin_patterns("/room/(?P<room_id>[^/]*)/media$")

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()

    async def on_GET(
        self, request: SynapseRequest, room_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        local_mxcs, remote_mxcs = await self.store.get_media_mxcs_in_room(room_id)

        return HTTPStatus.OK, {"local": local_mxcs, "remote": remote_mxcs}


class PurgeMediaCacheRestServlet(RestServlet):
    PATTERNS = admin_patterns("/purge_media_cache$")

    def __init__(self, hs: "HomeServer"):
        self.media_repository = hs.get_media_repository()
        self.auth = hs.get_auth()

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        before_ts = parse_integer(request, "before_ts", required=True)
        logger.info("before_ts: %r", before_ts)

        if before_ts < 0:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Query parameter before_ts must be a positive integer.",
                errcode=Codes.INVALID_PARAM,
            )
        elif before_ts < 30000000000:  # Dec 1970 in milliseconds, Aug 2920 in seconds
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Query parameter before_ts you provided is from the year 1970. "
                + "Double check that you are providing a timestamp in milliseconds.",
                errcode=Codes.INVALID_PARAM,
            )

        ret = await self.media_repository.delete_old_remote_media(before_ts)

        return HTTPStatus.OK, ret


class DeleteMediaByID(RestServlet):
    """Delete local media by a given ID. Removes it from this server."""

    PATTERNS = admin_patterns("/media/(?P<server_name>[^/]*)/(?P<media_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()
        self._is_mine_server_name = hs.is_mine_server_name
        self.media_repository = hs.get_media_repository()

    async def on_DELETE(
        self, request: SynapseRequest, server_name: str, media_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        if not self._is_mine_server_name(server_name):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only delete local media")

        if await self.store.get_local_media(media_id) is None:
            raise NotFoundError("Unknown media")

        logging.info("Deleting local media by ID: %s", media_id)

        deleted_media, total = await self.media_repository.delete_local_media_ids(
            [media_id]
        )
        return HTTPStatus.OK, {"deleted_media": deleted_media, "total": total}


class DeleteMediaByDateSize(RestServlet):
    """Delete local media and local copies of remote media by
    timestamp and size.
    """

    PATTERNS = [
        *admin_patterns("/media/delete$"),
        # This URL kept around for legacy reasons, it is undesirable since it
        # overlaps with the DeleteMediaByID servlet.
        *admin_patterns("/media/(?P<server_name>[^/]*)/delete$"),
    ]

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.auth = hs.get_auth()
        self.server_name = hs.hostname
        self.media_repository = hs.get_media_repository()

    async def on_POST(
        self, request: SynapseRequest, server_name: Optional[str] = None
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self.auth, request)

        before_ts = parse_integer(request, "before_ts", required=True)
        size_gt = parse_integer(request, "size_gt", default=0)
        keep_profiles = parse_boolean(request, "keep_profiles", default=True)

        if before_ts < 30000000000:  # Dec 1970 in milliseconds, Aug 2920 in seconds
            raise SynapseError(
                HTTPStatus.BAD_REQUEST,
                "Query parameter before_ts you provided is from the year 1970. "
                + "Double check that you are providing a timestamp in milliseconds.",
                errcode=Codes.INVALID_PARAM,
            )

        # This check is useless, we keep it for the legacy endpoint only.
        if server_name is not None and self.server_name != server_name:
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only delete local media")

        logging.info(
            "Deleting local media by timestamp: %s, size larger than: %s, keep profile media: %s"
            % (before_ts, size_gt, keep_profiles)
        )

        deleted_media, total = await self.media_repository.delete_old_local_media(
            before_ts, size_gt, keep_profiles
        )
        return HTTPStatus.OK, {"deleted_media": deleted_media, "total": total}


class UserMediaRestServlet(RestServlet):
    """
    Gets information about all uploaded local media for a specific `user_id`.
    With DELETE request you can delete all this media.

    Example:
        http://localhost:8008/_synapse/admin/v1/users/@user:server/media

    Args:
        The parameters `from` and `limit` are required for pagination.
        By default, a `limit` of 100 is used.
    Returns:
        A list of media and an integer representing the total number of
        media that exist given for this user
    """

    PATTERNS = admin_patterns("/users/(?P<user_id>[^/]*)/media$")

    def __init__(self, hs: "HomeServer"):
        self.is_mine = hs.is_mine
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self.media_repository = hs.get_media_repository()

    async def on_GET(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        # This will always be set by the time Twisted calls us.
        assert request.args is not None

        await assert_requester_is_admin(self.auth, request)

        if not self.is_mine(UserID.from_string(user_id)):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only look up local users")

        user = await self.store.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Unknown user")

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)

        # If neither `order_by` nor `dir` is set, set the default order
        # to newest media is on top for backward compatibility.
        if b"order_by" not in request.args and b"dir" not in request.args:
            order_by = MediaSortOrder.CREATED_TS.value
            direction = Direction.BACKWARDS
        else:
            order_by = parse_string(
                request,
                "order_by",
                default=MediaSortOrder.CREATED_TS.value,
                allowed_values=[sort_order.value for sort_order in MediaSortOrder],
            )
            direction = parse_enum(
                request, "dir", Direction, default=Direction.FORWARDS
            )

        media, total = await self.store.get_local_media_by_user_paginate(
            start, limit, user_id, order_by, direction
        )

        ret = {"media": [attr.asdict(m) for m in media], "total": total}
        if (start + limit) < total:
            ret["next_token"] = start + len(media)

        return HTTPStatus.OK, ret

    async def on_DELETE(
        self, request: SynapseRequest, user_id: str
    ) -> Tuple[int, JsonDict]:
        # This will always be set by the time Twisted calls us.
        assert request.args is not None

        await assert_requester_is_admin(self.auth, request)

        if not self.is_mine(UserID.from_string(user_id)):
            raise SynapseError(HTTPStatus.BAD_REQUEST, "Can only look up local users")

        user = await self.store.get_user_by_id(user_id)
        if user is None:
            raise NotFoundError("Unknown user")

        start = parse_integer(request, "from", default=0)
        limit = parse_integer(request, "limit", default=100)

        # If neither `order_by` nor `dir` is set, set the default order
        # to newest media is on top for backward compatibility.
        if b"order_by" not in request.args and b"dir" not in request.args:
            order_by = MediaSortOrder.CREATED_TS.value
            direction = Direction.BACKWARDS
        else:
            order_by = parse_string(
                request,
                "order_by",
                default=MediaSortOrder.CREATED_TS.value,
                allowed_values=[sort_order.value for sort_order in MediaSortOrder],
            )
            direction = parse_enum(
                request, "dir", Direction, default=Direction.FORWARDS
            )

        media, _ = await self.store.get_local_media_by_user_paginate(
            start, limit, user_id, order_by, direction
        )

        deleted_media, total = await self.media_repository.delete_local_media_ids(
            [m.media_id for m in media]
        )

        return HTTPStatus.OK, {"deleted_media": deleted_media, "total": total}


def register_servlets_for_media_repo(hs: "HomeServer", http_server: HttpServer) -> None:
    """
    Media repo specific APIs.
    """
    PurgeMediaCacheRestServlet(hs).register(http_server)
    QuarantineMediaInRoom(hs).register(http_server)
    QuarantineMediaByID(hs).register(http_server)
    UnquarantineMediaByID(hs).register(http_server)
    QuarantineMediaByUser(hs).register(http_server)
    ProtectMediaByID(hs).register(http_server)
    UnprotectMediaByID(hs).register(http_server)
    ListMediaInRoom(hs).register(http_server)
    # XXX DeleteMediaByDateSize must be registered before DeleteMediaByID as
    #     their URL routes overlap.
    DeleteMediaByDateSize(hs).register(http_server)
    DeleteMediaByID(hs).register(http_server)
    UserMediaRestServlet(hs).register(http_server)
