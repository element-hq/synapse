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

"""Default implementation of the MatrixRTC LiveKit endpoints from MSC4195. Used when
no application service is configured for proxying them."""

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import (
    Codes,
    FederationDeniedError,
    HttpResponseException,
    RequestSendFailed,
    SynapseError,
)
from synapse.api.urls import FEDERATION_UNSTABLE_PREFIX, FEDERATION_V1_PREFIX
from synapse.http.client import is_unknown_endpoint
from synapse.types import JsonDict, Requester
from synapse.util.stringutils import parse_and_validate_server_name

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

GET_TOKEN_PATH = "/rtc/livekit/get_token"
FEDERATION_MSC4195_UNSTABLE_PREFIX = FEDERATION_UNSTABLE_PREFIX + "/io.element.msc4195"

# The slot ID of room-wide `m.call` sessions and the unstable form of it that is
# expected by servers which only implement the unstable endpoints.
M_CALL_ROOM_SLOT_ID = "m.call#room"
M_CALL_ROOM_SLOT_ID_UNSTABLE = "m.call#ROOM"


def no_local_sfu_error() -> SynapseError:
    """The error for requests targeting this server, which has no SFU."""
    return SynapseError(
        HTTPStatus.BAD_REQUEST,
        "The request `url` does not match a LiveKit SFU of this server",
        Codes.INVALID_PARAM,
    )


def _forbidden_error() -> SynapseError:
    """The error for requests that fail the room membership check."""
    # The error is deliberately generic so that it doesn't reveal whose
    # membership check failed.
    return SynapseError(
        HTTPStatus.FORBIDDEN,
        "Access to this room is not permitted",
        Codes.FORBIDDEN,
    )


def _bad_gateway_error() -> SynapseError:
    """The error for requests that failed to be answered by the destination server."""
    return SynapseError(
        HTTPStatus.BAD_GATEWAY,
        "Unable to reach the specified homeserver",
        Codes.UNKNOWN,
    )


class MatrixRtcLiveKitHandler:
    def __init__(self, hs: "HomeServer"):
        self._is_mine_server_name = hs.is_mine_server_name
        self._store = hs.get_datastores().main
        self._federation_client = hs.get_federation_http_client()

    async def get_token(
        self,
        requester: Requester,
        server_name: str | None,
        url: str,
        room_id: str,
        slot_id: str,
        member_id: str,
    ) -> str:
        """Handles a `/rtc/livekit/get_token` Client-Server request.

        By default, a server has no SFU of its own, so only requests for the SFU of a
        remote server can succeed. These are relayed to that server over federation.

        Args:
            requester: The user requesting the token.
            server_name: The server whose SFU the token is requested for. Defaults to
                this server if missing.
            url: The WebSocket URL of the SFU.
            room_id: The room of the MatrixRTC session.
            slot_id: The slot of the MatrixRTC session.
            member_id: The `member.id` of the requester's `m.rtc.member` event.

        Returns:
            The LiveKit access token.

        Raises:
            SynapseError: 400 `M_INVALID_PARAM` if the request targets this server
                or the remote server rejected `url`, 403 `M_FORBIDDEN` if the
                requester or the remote server is not joined to the room or the
                remote server denied the request, and 502 `M_UNKNOWN` if the remote
                server failed to provide a token.
        """
        if not server_name or self._is_mine_server_name(server_name):
            raise no_local_sfu_error()

        try:
            parse_and_validate_server_name(server_name)
        except ValueError:
            raise SynapseError(
                HTTPStatus.BAD_REQUEST, "Invalid `server_name`", Codes.INVALID_PARAM
            )

        user_id = requester.user.to_string()

        if not await self._store.check_local_user_in_room(user_id, room_id):
            logger.info("%s is not joined to %s", user_id, room_id)
            raise _forbidden_error()

        if not await self._store.is_host_joined(room_id, server_name):
            logger.info("%s is not joined to %s", server_name, room_id)
            raise _forbidden_error()

        content = {
            "user_id": user_id,
            "url": url,
            "room_id": room_id,
            "slot_id": slot_id,
            "member_id": member_id,
        }

        try:
            response = await self._request_token(server_name, content)
        except HttpResponseException as e:
            errcode = e.to_synapse_error().errcode
            logger.info(
                "%s rejected the token request of %s for %s: %d %s",
                server_name,
                user_id,
                room_id,
                e.code,
                errcode,
            )
            # MSC4195 mandates that these two errors are relayed and that any
            # other error results in a 502.
            if e.code == HTTPStatus.FORBIDDEN and errcode == Codes.FORBIDDEN:
                raise _forbidden_error()
            if e.code == HTTPStatus.BAD_REQUEST and errcode == Codes.INVALID_PARAM:
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "The destination homeserver rejected the specified `url`",
                    Codes.INVALID_PARAM,
                )
            raise _bad_gateway_error()
        except (RequestSendFailed, FederationDeniedError) as e:
            logger.warning(
                "Failed to request a token from %s for %s in %s: %s",
                server_name,
                user_id,
                room_id,
                e,
            )
            raise _bad_gateway_error()

        jwt = response.get("jwt") if isinstance(response, dict) else None
        if not isinstance(jwt, str):
            logger.warning("%s responded without a `jwt`", server_name)
            raise _bad_gateway_error()

        return jwt

    async def _request_token(self, destination: str, content: JsonDict) -> JsonDict:
        """Requests a token from the `/rtc/livekit/get_token` federation endpoint of
        `destination`, falling back to the unstable endpoint if the stable one is
        not recognised."""
        try:
            return await self._federation_client.send_direct_request(
                "POST",
                destination,
                FEDERATION_V1_PREFIX + GET_TOKEN_PATH,
                json_body=content,
            )
        except HttpResponseException as e:
            if not is_unknown_endpoint(e):
                raise

        logger.debug(
            "%s does not recognise the stable get_token endpoint, "
            "retrying on the unstable one",
            destination,
        )

        # The accepted form of MSC4196 uses a slot ID of `m.call#room` whereas the
        # unstable form used `m.call#ROOM`. Servers that only support the unstable
        # endpoint, very likely also still use the unstable slot ID. If our client
        # sent the stable slot ID, we map it back to the unstable. This matches what
        # clients of the remote server should send to the unstable endpoint and
        # ensures that both our and their clients end up in the same room on the SFU.
        if content["slot_id"] == M_CALL_ROOM_SLOT_ID:
            content = {**content, "slot_id": M_CALL_ROOM_SLOT_ID_UNSTABLE}

        return await self._federation_client.send_direct_request(
            "POST",
            destination,
            FEDERATION_MSC4195_UNSTABLE_PREFIX + GET_TOKEN_PATH,
            json_body=content,
        )
