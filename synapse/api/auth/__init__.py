#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation.
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
from typing import TYPE_CHECKING, Optional, Tuple

from typing_extensions import Protocol

from twisted.web.server import Request

from synapse.appservice import ApplicationService
from synapse.http.site import SynapseRequest
from synapse.types import Requester

if TYPE_CHECKING:
    from synapse.rest.admin.experimental_features import ExperimentalFeature

# guests always get this device id.
GUEST_DEVICE_ID = "guest_device"


class Auth(Protocol):
    """The interface that an auth provider must implement."""

    async def check_user_in_room(
        self,
        room_id: str,
        requester: Requester,
        allow_departed_users: bool = False,
    ) -> Tuple[str, Optional[str]]:
        """Check if the user is in the room, or was at some point.
        Args:
            room_id: The room to check.

            user_id: The user to check.

            current_state: Optional map of the current state of the room.
                If provided then that map is used to check whether they are a
                member of the room. Otherwise the current membership is
                loaded from the database.

            allow_departed_users: if True, accept users that were previously
                members but have now departed.

        Raises:
            AuthError if the user is/was not in the room.
        Returns:
            The current membership of the user in the room and the
            membership event ID of the user.
        """

    async def get_user_by_req(
        self,
        request: SynapseRequest,
        allow_guest: bool = False,
        allow_expired: bool = False,
        allow_locked: bool = False,
    ) -> Requester:
        """Get a registered user's ID.

        Args:
            request: An HTTP request with an access_token query parameter.
            allow_guest: If False, will raise an AuthError if the user making the
                request is a guest.
            allow_expired: If True, allow the request through even if the account
                is expired, or session token lifetime has ended. Note that
                /login will deliver access tokens regardless of expiration.

        Returns:
            Resolves to the requester
        Raises:
            InvalidClientCredentialsError if no user by that token exists or the token
                is invalid.
            AuthError if access is denied for the user in the access token
        """

    async def get_user_by_req_experimental_feature(
        self,
        request: SynapseRequest,
        feature: "ExperimentalFeature",
        allow_guest: bool = False,
        allow_expired: bool = False,
        allow_locked: bool = False,
    ) -> Requester:
        """Like `get_user_by_req`, except also checks if the user has access to
        the experimental feature. If they don't returns a 404 unrecognized
        request.
        """

    async def validate_appservice_can_control_user_id(
        self, app_service: ApplicationService, user_id: str
    ) -> None:
        """Validates that the app service is allowed to control
        the given user.

        Args:
            app_service: The app service that controls the user
            user_id: The author MXID that the app service is controlling

        Raises:
            AuthError: If the application service is not allowed to control the user
                (user namespace regex does not match, wrong homeserver, etc)
                or if the user has not been registered yet.
        """

    async def get_user_by_access_token(
        self,
        token: str,
        allow_expired: bool = False,
    ) -> Requester:
        """Validate access token and get user_id from it

        Args:
            token: The access token to get the user by
            allow_expired: If False, raises an InvalidClientTokenError
                if the token is expired

        Raises:
            InvalidClientTokenError if a user by that token exists, but the token is
                expired
            InvalidClientCredentialsError if no user by that token exists or the token
                is invalid
        """

    async def is_server_admin(self, requester: Requester) -> bool:
        """Check if the given user is a local server admin.

        Args:
            requester: user to check

        Returns:
            True if the user is an admin
        """

    async def check_can_change_room_list(
        self, room_id: str, requester: Requester
    ) -> bool:
        """Determine whether the user is allowed to edit the room's entry in the
        published room list.

        Args:
            room_id
            user
        """

    @staticmethod
    def has_access_token(request: Request) -> bool:
        """Checks if the request has an access_token.

        Returns:
            False if no access_token was given, True otherwise.
        """

    @staticmethod
    def get_access_token_from_request(request: Request) -> str:
        """Extracts the access_token from the request.

        Args:
            request: The http request.
        Returns:
            The access_token
        Raises:
            MissingClientTokenError: If there isn't a single access_token in the
                request
        """

    async def check_user_in_room_or_world_readable(
        self, room_id: str, requester: Requester, allow_departed_users: bool = False
    ) -> Tuple[str, Optional[str]]:
        """Checks that the user is or was in the room or the room is world
        readable. If it isn't then an exception is raised.

        Args:
            room_id: room to check
            user_id: user to check
            allow_departed_users: if True, accept users that were previously
                members but have now departed

        Returns:
            Resolves to the current membership of the user in the room and the
            membership event ID of the user. If the user is not in the room and
            never has been, then `(Membership.JOIN, None)` is returned.
        """
