#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
import itertools
import logging
from typing import TYPE_CHECKING

from synapse.api.constants import Membership
from synapse.api.errors import SynapseError
from synapse.replication.http.deactivate_account import (
    ReplicationNotifyAccountDeactivatedServlet,
)
from synapse.types import Codes, Requester, UserID, create_requester

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class DeactivateAccountHandler:
    """Handler which deals with deactivating user accounts."""

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.hs = hs
        self.server_name = hs.hostname
        self._auth_handler = hs.get_auth_handler()
        self._device_handler = hs.get_device_handler()
        self._room_member_handler = hs.get_room_member_handler()
        self._identity_handler = hs.get_identity_handler()
        self._profile_handler = hs.get_profile_handler()
        self._pusher_pool = hs.get_pusherpool()
        self.user_directory_handler = hs.get_user_directory_handler()
        self._server_name = hs.hostname
        self._third_party_rules = hs.get_module_api_callbacks().third_party_event_rules

        # Flag that indicates whether the process to part users from rooms is running
        self._user_parter_running = False
        self._third_party_rules = hs.get_module_api_callbacks().third_party_event_rules

        self._notify_account_deactivated_client = None

        # Start the user parter loop so it can resume parting users from rooms where
        # it left off (if it has work left to do).
        if hs.config.worker.worker_app is None:
            hs.get_clock().call_when_running(self._start_user_parting)
        else:
            self._notify_account_deactivated_client = (
                ReplicationNotifyAccountDeactivatedServlet.make_client(hs)
            )

        self._account_validity_enabled = (
            hs.config.account_validity.account_validity_enabled
        )

    async def deactivate_account(
        self,
        user_id: str,
        erase_data: bool,
        requester: Requester,
        id_server: str | None = None,
        by_admin: bool = False,
    ) -> bool:
        """Deactivate a user's account

        Args:
            user_id: ID of user to be deactivated
            erase_data: whether to GDPR-erase the user's data
            requester: The user attempting to make this change.
            id_server: Use the given identity server when unbinding
                any threepids. If None then will attempt to unbind using the
                identity server specified when binding (if known).
            by_admin: Whether this change was made by an administrator.

        Returns:
            True if identity server supports removing threepids, otherwise False.
        """
        # Check if this user can be deactivated
        if not await self._third_party_rules.check_can_deactivate_user(
            user_id, by_admin
        ):
            raise SynapseError(
                403, "Deactivation of this user is forbidden", Codes.FORBIDDEN
            )

        logger.info(
            "%s requested deactivation of %s erase_data=%s id_server=%s",
            requester.user,
            user_id,
            erase_data,
            id_server,
        )

        # FIXME: Theoretically there is a race here wherein user resets
        # password using threepid.

        # delete threepids first. We remove these from the IS so if this fails,
        # leave the user still active so they can try again.
        # Ideally we would prevent password resets and then do this in the
        # background thread.

        # This will be set to false if the identity server doesn't support
        # unbinding
        identity_server_supports_unbinding = True

        # Attempt to unbind any known bound threepids to this account from identity
        # server(s).
        bound_threepids = await self.store.user_get_bound_threepids(user_id)
        for medium, address in bound_threepids:
            try:
                result = await self._identity_handler.try_unbind_threepid(
                    user_id, medium, address, id_server
                )
            except Exception:
                # Do we want this to be a fatal error or should we carry on?
                logger.exception("Failed to remove threepid from ID server")
                raise SynapseError(400, "Failed to remove threepid from ID server")

            identity_server_supports_unbinding &= result

        # Remove any local threepid associations for this account.
        local_threepids = await self.store.user_get_threepids(user_id)
        for local_threepid in local_threepids:
            await self._auth_handler.delete_local_threepid(
                user_id, local_threepid.medium, local_threepid.address
            )

        # delete any devices belonging to the user, which will also
        # delete corresponding access tokens.
        await self._device_handler.delete_all_devices_for_user(user_id)
        # then delete any remaining access tokens which weren't associated with
        # a device.
        await self._auth_handler.delete_access_tokens_for_user(user_id)

        await self.store.user_set_password_hash(user_id, None)

        # Most of the pushers will have been deleted when we logged out the
        # associated devices above, but we still need to delete pushers not
        # associated with devices, e.g. email pushers.
        await self._pusher_pool.delete_all_pushers_for_user(user_id)

        # Add the user to a table of users pending deactivation (ie.
        # removal from all the rooms they're a member of)
        await self.store.add_user_pending_deactivation(user_id)

        # delete from user directory
        await self.user_directory_handler.handle_local_user_deactivated(user_id)

        # Mark the user as erased, if they asked for that
        if erase_data:
            user = UserID.from_string(user_id)
            # Remove avatar URL from this user
            await self._profile_handler.set_avatar_url(
                user, requester, "", by_admin, deactivation=True
            )
            # Remove displayname from this user
            await self._profile_handler.set_displayname(
                user, requester, "", by_admin, deactivation=True
            )

            logger.info("Marking %s as erased", user_id)
            await self.store.mark_user_erased(user_id)

        # Reject all pending invites and knocks for the user, so that the
        # user doesn't show up in the "invited" section of rooms' members list.
        await self._reject_pending_invites_and_knocks_for_user(user_id)

        # Remove all information on the user from the account_validity table.
        if self._account_validity_enabled:
            await self.store.delete_account_validity_for_user(user_id)

        # Mark the user as deactivated.
        await self.store.set_user_deactivated_status(user_id, True)

        # Remove account data (including ignored users and push rules).
        await self.store.purge_account_data_for_user(user_id)

        # Remove thread subscriptions for the user
        await self.store.purge_thread_subscription_settings_for_user(user_id)

        # Delete any server-side backup keys
        await self.store.bulk_delete_backup_keys_and_versions_for_user(user_id)

        # Notify modules and start the room parting process.
        await self.notify_account_deactivated(user_id, by_admin=by_admin)

        return identity_server_supports_unbinding

    async def notify_account_deactivated(
        self,
        user_id: str,
        by_admin: bool = False,
    ) -> None:
        """Notify modules and start the room parting process.
        Goes through replication if this is not the main process.
        """
        if self._notify_account_deactivated_client is not None:
            await self._notify_account_deactivated_client(
                user_id=user_id,
                by_admin=by_admin,
            )
            return

        # Now start the process that goes through that list and
        # parts users from rooms (if it isn't already running)
        self._start_user_parting()

        # Let modules know the user has been deactivated.
        await self._third_party_rules.on_user_deactivation_status_changed(
            user_id,
            True,
            by_admin=by_admin,
        )

    async def _reject_pending_invites_and_knocks_for_user(self, user_id: str) -> None:
        """Reject pending invites and knocks addressed to a given user ID.

        Args:
            user_id: The user ID to reject pending invites and knocks for.
        """
        user = UserID.from_string(user_id)
        pending_invites = await self.store.get_invited_rooms_for_local_user(user_id)
        pending_knocks = await self.store.get_knocked_at_rooms_for_local_user(user_id)

        for room in itertools.chain(pending_invites, pending_knocks):
            try:
                await self._room_member_handler.update_membership(
                    create_requester(user, authenticated_entity=self._server_name),
                    user,
                    room.room_id,
                    Membership.LEAVE,
                    ratelimit=False,
                    require_consent=False,
                )
                logger.info(
                    "Rejected %r for deactivated user %r in room %r",
                    room.membership,
                    user_id,
                    room.room_id,
                )
            except Exception:
                logger.exception(
                    "Failed to reject %r for user %r in room %r:"
                    " ignoring and continuing",
                    room.membership,
                    user_id,
                    room.room_id,
                )

    def _start_user_parting(self) -> None:
        """
        Start the process that goes through the table of users
        pending deactivation, if it isn't already running.
        """
        if not self._user_parter_running:
            self.hs.run_as_background_process(
                "user_parter_loop", self._user_parter_loop
            )

    async def _user_parter_loop(self) -> None:
        """Loop that parts deactivated users from rooms"""
        self._user_parter_running = True
        logger.info("Starting user parter")
        try:
            while True:
                user_id = await self.store.get_user_pending_deactivation()
                if user_id is None:
                    break
                logger.info("User parter parting %r", user_id)
                await self._part_user(user_id)
                await self.store.del_user_pending_deactivation(user_id)
                logger.info("User parter finished parting %r", user_id)
            logger.info("User parter finished: stopping")
        finally:
            self._user_parter_running = False

    async def _part_user(self, user_id: str) -> None:
        """Causes the given user_id to leave all the rooms they're joined to"""
        user = UserID.from_string(user_id)

        rooms_for_user = await self.store.get_rooms_for_user(user_id)
        requester = create_requester(user, authenticated_entity=self._server_name)
        should_erase = await self.store.is_user_erased(user_id)

        for room_id in rooms_for_user:
            logger.info("User parter parting %r from %r", user_id, room_id)
            try:
                # Before parting the user, redact all membership events if requested
                if should_erase:
                    event_ids = await self.store.get_membership_event_ids_for_user(
                        user_id, room_id
                    )
                    for event_id in event_ids:
                        await self.store.expire_event(event_id)

                await self._room_member_handler.update_membership(
                    requester,
                    user,
                    room_id,
                    "leave",
                    ratelimit=False,
                    require_consent=False,
                )

                # Mark the room forgotten too, because they won't be able to do this
                # for us. This may lead to the room being purged eventually.
                await self._room_member_handler.forget(user, room_id)
            except Exception:
                logger.exception(
                    "Failed to part user %r from room %r: ignoring and continuing",
                    user_id,
                    room_id,
                )

    async def activate_account(self, user_id: str) -> None:
        """
        Activate an account that was previously deactivated.

        This marks the user as active and not erased in the database, but does
        not attempt to rejoin rooms, re-add threepids, etc.

        If enabled, the user will be re-added to the user directory.

        The user will also need a password hash set to actually login.

        Args:
            user_id: ID of user to be re-activated
        """
        user = UserID.from_string(user_id)

        # Ensure the user is not marked as erased.
        await self.store.mark_user_not_erased(user_id)

        # Mark the user as active.
        await self.store.set_user_deactivated_status(user_id, False)

        await self._third_party_rules.on_user_deactivation_status_changed(
            user_id, False, True
        )

        # Add the user to the directory, if necessary. Note that
        # this must be done after the user is re-activated, because
        # deactivated users are excluded from the user directory.
        profile = await self.store.get_profileinfo(user)
        await self.user_directory_handler.handle_local_profile_change(user_id, profile)
