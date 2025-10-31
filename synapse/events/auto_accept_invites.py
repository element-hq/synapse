#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C
# Copyright (C) 2024 New Vector, Ltd
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
from typing import Any

from synapse.api.constants import AccountDataTypes, EventTypes, Membership
from synapse.api.errors import SynapseError
from synapse.config.auto_accept_invites import AutoAcceptInvitesConfig
from synapse.module_api import EventBase, ModuleApi, run_as_background_process

logger = logging.getLogger(__name__)


class InviteAutoAccepter:
    def __init__(self, config: AutoAcceptInvitesConfig, api: ModuleApi):
        # Keep a reference to the Module API.
        self._api = api
        self.server_name = api.server_name
        self._config = config

        if not self._config.enabled:
            return

        should_run_on_this_worker = config.worker_to_run_on == self._api.worker_name

        if not should_run_on_this_worker:
            logger.info(
                "Not accepting invites on this worker (configured: %r, here: %r)",
                config.worker_to_run_on,
                self._api.worker_name,
            )
            return

        logger.info(
            "Accepting invites on this worker (here: %r)", self._api.worker_name
        )

        # Register the callback.
        self._api.register_third_party_rules_callbacks(
            on_new_event=self.on_new_event,
        )

    async def on_new_event(self, event: EventBase, *args: Any) -> None:
        """Listens for new events, and if the event is an invite for a local user then
        automatically accepts it.

        Args:
            event: The incoming event.
        """
        # Check if the event is an invite for a local user.
        if (
            event.type != EventTypes.Member
            or event.is_state() is False
            or event.membership != Membership.INVITE
            or self._api.is_mine(event.state_key) is False
        ):
            return

        # Only accept invites for direct messages if the configuration mandates it.
        is_direct_message = event.content.get("is_direct", False)
        if (
            self._config.accept_invites_only_for_direct_messages
            and is_direct_message is False
        ):
            return

        # Only accept invites from remote users if the configuration mandates it.
        is_from_local_user = self._api.is_mine(event.sender)
        if (
            self._config.accept_invites_only_from_local_users
            and is_from_local_user is False
        ):
            return

        # Check the user is activated.
        recipient = await self._api.get_userinfo_by_id(event.state_key)

        # Ignore if the user doesn't exist.
        if recipient is None:
            return

        # Never accept invites for deactivated users.
        if recipient.is_deactivated:
            return

        # Never accept invites for suspended users.
        if recipient.suspended:
            return

        # Never accept invites for locked users.
        if recipient.locked:
            return

        # Make the user join the room. We run this as a background process to circumvent a race condition
        # that occurs when responding to invites over federation (see https://github.com/matrix-org/synapse-auto-accept-invite/issues/12)
        run_as_background_process(
            "retry_make_join",
            self._retry_make_join,
            event.state_key,
            event.state_key,
            event.room_id,
            "join",
        )

        if is_direct_message:
            # Mark this room as a direct message!
            await self._mark_room_as_direct_message(
                event.state_key, event.sender, event.room_id
            )

    async def _mark_room_as_direct_message(
        self, user_id: str, dm_user_id: str, room_id: str
    ) -> None:
        """
        Marks a room (`room_id`) as a direct message with the counterparty `dm_user_id`
        from the perspective of the user `user_id`.

        Args:
            user_id: the user for whom the membership is changing
            dm_user_id: the user performing the membership change
            room_id: room id of the room the user is invited to
        """

        # This is a dict of User IDs to tuples of Room IDs
        # (get_global will return a frozendict of tuples as it freezes the data,
        # but we should accept either frozen or unfrozen variants.)
        # Be careful: we convert the outer frozendict into a dict here,
        # but the contents of the dict are still frozen (tuples in lieu of lists,
        # etc.)
        dm_map: dict[str, tuple[str, ...]] = dict(
            await self._api.account_data_manager.get_global(
                user_id, AccountDataTypes.DIRECT
            )
            or {}
        )

        if dm_user_id not in dm_map:
            dm_map[dm_user_id] = (room_id,)
        else:
            dm_rooms_for_user = dm_map[dm_user_id]
            assert isinstance(dm_rooms_for_user, (tuple, list))

            dm_map[dm_user_id] = tuple(dm_rooms_for_user) + (room_id,)

        await self._api.account_data_manager.put_global(
            user_id, AccountDataTypes.DIRECT, dm_map
        )

    async def _retry_make_join(
        self, sender: str, target: str, room_id: str, new_membership: str
    ) -> None:
        """
        A function to retry sending the `make_join` request with an increasing backoff. This is
        implemented to work around a race condition when receiving invites over federation.

        Args:
            sender: the user performing the membership change
            target: the user for whom the membership is changing
            room_id: room id of the room to join to
            new_membership: the type of membership event (in this case will be "join")
        """

        sleep = 0
        retries = 0
        join_event = None

        while retries < 5:
            try:
                await self._api.sleep(sleep)
                join_event = await self._api.update_room_membership(
                    sender=sender,
                    target=target,
                    room_id=room_id,
                    new_membership=new_membership,
                )
            except SynapseError as e:
                if e.code == HTTPStatus.FORBIDDEN:
                    logger.debug(
                        "Update_room_membership was forbidden. This can sometimes be expected for remote invites. Exception: %s",
                        e,
                    )
                else:
                    logger.warning(
                        "Update_room_membership raised the following unexpected (SynapseError) exception: %s",
                        e,
                    )
            except Exception as e:
                logger.warning(
                    "Update_room_membership raised the following unexpected exception: %s",
                    e,
                )

            sleep = 2**retries
            retries += 1

            if join_event is not None:
                break
