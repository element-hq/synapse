#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from typing import TYPE_CHECKING

from synapse.api.errors import Codes, SynapseError
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


class AccountHandler:
    def __init__(self, hs: "HomeServer"):
        self._main_store = hs.get_datastores().main
        self._is_mine = hs.is_mine
        self._federation_client = hs.get_federation_client()
        self._use_account_validity_in_account_status = (
            hs.config.server.use_account_validity_in_account_status
        )
        self._account_validity_handler = hs.get_account_validity_handler()

    async def get_account_statuses(
        self,
        user_ids: list[str],
        allow_remote: bool,
    ) -> tuple[JsonDict, list[str]]:
        """Get account statuses for a list of user IDs.

        If one or more account(s) belong to remote homeservers, retrieve their status(es)
        over federation if allowed.

        Args:
            user_ids: The list of accounts to retrieve the status of.
            allow_remote: Whether to try to retrieve the status of remote accounts, if
                any.

        Returns:
            The account statuses as well as the list of users whose statuses could not be
            retrieved.

        Raises:
            SynapseError if a required parameter is missing or malformed, or if one of
            the accounts isn't local to this homeserver and allow_remote is False.
        """
        statuses = {}
        failures = []
        remote_users: list[UserID] = []

        for raw_user_id in user_ids:
            try:
                user_id = UserID.from_string(raw_user_id)
            except SynapseError:
                raise SynapseError(
                    400,
                    f"Not a valid Matrix user ID: {raw_user_id}",
                    Codes.INVALID_PARAM,
                )

            if self._is_mine(user_id):
                status = await self._get_local_account_status(user_id)
                statuses[user_id.to_string()] = status
            else:
                if not allow_remote:
                    raise SynapseError(
                        400,
                        f"Not a local user: {raw_user_id}",
                        Codes.INVALID_PARAM,
                    )

                remote_users.append(user_id)

        if allow_remote and len(remote_users) > 0:
            remote_statuses, remote_failures = await self._get_remote_account_statuses(
                remote_users,
            )

            statuses.update(remote_statuses)
            failures += remote_failures

        return statuses, failures

    async def _get_local_account_status(self, user_id: UserID) -> JsonDict:
        """Retrieve the status of a local account.

        Args:
            user_id: The account to retrieve the status of.

        Returns:
            The account's status.
        """
        status = {"exists": False}

        userinfo = await self._main_store.get_user_by_id(user_id.to_string())

        if userinfo is not None:
            status = {
                "exists": True,
                "deactivated": userinfo.is_deactivated,
            }

            if self._use_account_validity_in_account_status:
                status[
                    "org.matrix.expired"
                ] = await self._account_validity_handler.is_user_expired(
                    user_id.to_string()
                )

        return status

    async def _get_remote_account_statuses(
        self, remote_users: list[UserID]
    ) -> tuple[JsonDict, list[str]]:
        """Send out federation requests to retrieve the statuses of remote accounts.

        Args:
            remote_users: The accounts to retrieve the statuses of.

        Returns:
            The statuses of the accounts, and a list of accounts for which no status
            could be retrieved.
        """
        # Group remote users by destination, so we only send one request per remote
        # homeserver.
        by_destination: dict[str, list[str]] = {}
        for user in remote_users:
            if user.domain not in by_destination:
                by_destination[user.domain] = []

            by_destination[user.domain].append(user.to_string())

        # Retrieve the statuses and failures for remote accounts.
        final_statuses: JsonDict = {}
        final_failures: list[str] = []
        for destination, users in by_destination.items():
            statuses, failures = await self._federation_client.get_account_status(
                destination,
                users,
            )

            final_statuses.update(statuses)
            final_failures += failures

        return final_statuses, final_failures
