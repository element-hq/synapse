#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from typing import TYPE_CHECKING, List, Tuple

from synapse.api.constants import (
    EventTypes,
    LimitBlockingTypes,
    ServerNoticeLimitReached,
    ServerNoticeMsgType,
)
from synapse.api.errors import AuthError, ResourceLimitError, SynapseError

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ResourceLimitsServerNotices:
    """Keeps track of whether the server has reached it's resource limit and
    ensures that the client is kept up to date.
    """

    def __init__(self, hs: "HomeServer"):
        self._server_notices_manager = hs.get_server_notices_manager()
        self._store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._auth_blocking = hs.get_auth_blocking()
        self._config = hs.config
        self._resouce_limited = False
        self._account_data_handler = hs.get_account_data_handler()
        self._message_handler = hs.get_message_handler()
        self._state = hs.get_state_handler()

        self._notifier = hs.get_notifier()

        self._enabled = (
            hs.config.server.limit_usage_by_mau
            and self._server_notices_manager.is_enabled()
            and not hs.config.server.hs_disabled
        )

    async def maybe_send_server_notice_to_user(self, user_id: str) -> None:
        """Check if we need to send a notice to this user, this will be true in
        two cases.
        1. The server has reached its limit does not reflect this
        2. The room state indicates that the server has reached its limit when
        actually the server is fine

        Args:
            user_id: user to check
        """
        if not self._enabled:
            return

        timestamp = await self._store.user_last_seen_monthly_active(user_id)
        if timestamp is None:
            # This user will be blocked from receiving the notice anyway.
            # In practice, not sure we can ever get here
            return

        # Check if there's a server notice room for this user.
        room_id = await self._server_notices_manager.maybe_get_notice_room_for_user(
            user_id
        )

        if room_id is not None:
            # Determine current state of room
            currently_blocked, ref_events = await self._is_room_currently_blocked(
                room_id
            )
        else:
            currently_blocked = False
            ref_events = []

        limit_msg = None
        limit_type = None
        try:
            # Normally should always pass in user_id to check_auth_blocking
            # if you have it, but in this case are checking what would happen
            # to other users if they were to arrive.
            await self._auth_blocking.check_auth_blocking()
        except ResourceLimitError as e:
            limit_msg = e.msg
            limit_type = e.limit_type

        try:
            if (
                limit_type == LimitBlockingTypes.MONTHLY_ACTIVE_USER
                and not self._config.server.mau_limit_alerting
            ):
                # We have hit the MAU limit, but MAU alerting is disabled:
                # reset room if necessary and return
                if currently_blocked:
                    await self._remove_limit_block_notification(user_id, ref_events)
                return

            if currently_blocked and not limit_msg:
                # Room is notifying of a block, when it ought not to be.
                await self._remove_limit_block_notification(user_id, ref_events)
            elif not currently_blocked and limit_msg:
                # Room is not notifying of a block, when it ought to be.
                await self._apply_limit_block_notification(
                    user_id,
                    limit_msg,
                    limit_type,  # type: ignore
                )
        except SynapseError as e:
            logger.error("Error sending resource limits server notice: %s", e)

    async def _remove_limit_block_notification(
        self, user_id: str, ref_events: List[str]
    ) -> None:
        """Utility method to remove limit block notifications from the server
        notices room.

        Args:
            user_id: user to notify
            ref_events: The event_ids of pinned events that are unrelated to
                limit blocking and need to be preserved.
        """
        content = {"pinned": ref_events}
        await self._server_notices_manager.send_notice(
            user_id, content, EventTypes.Pinned, ""
        )

    async def _apply_limit_block_notification(
        self, user_id: str, event_body: str, event_limit_type: str
    ) -> None:
        """Utility method to apply limit block notifications in the server
        notices room.

        Args:
            user_id: user to notify
            event_body: The human readable text that describes the block.
            event_limit_type: Specifies the type of block e.g. monthly active user
                limit has been exceeded.
        """
        content = {
            "body": event_body,
            "msgtype": ServerNoticeMsgType,
            "server_notice_type": ServerNoticeLimitReached,
            "admin_contact": self._config.server.admin_contact,
            "limit_type": event_limit_type,
        }
        event = await self._server_notices_manager.send_notice(
            user_id, content, EventTypes.Message
        )

        content = {"pinned": [event.event_id]}
        await self._server_notices_manager.send_notice(
            user_id, content, EventTypes.Pinned, ""
        )

    async def _is_room_currently_blocked(self, room_id: str) -> Tuple[bool, List[str]]:
        """
        Determines if the room is currently blocked

        Args:
            room_id: The room id of the server notices room

        Returns:
            Tuple of:
                Is the room currently blocked

                The list of pinned event IDs that are unrelated to limit blocking
                This list can be used as a convenience in the case where the block
                is to be lifted and the remaining pinned event references need to be
                preserved
        """
        currently_blocked = False
        pinned_state_event = None
        try:
            pinned_state_event = (
                await self._storage_controllers.state.get_current_state_event(
                    room_id, event_type=EventTypes.Pinned, state_key=""
                )
            )
        except AuthError:
            # The user has yet to join the server notices room
            pass

        referenced_events: List[str] = []
        if pinned_state_event is not None:
            referenced_events = list(pinned_state_event.content.get("pinned", []))

        events = await self._store.get_events(referenced_events)
        for event_id, event in events.items():
            if event.type != EventTypes.Message:
                continue
            if event.content.get("msgtype") == ServerNoticeMsgType:
                currently_blocked = True
                # remove event in case we need to disable blocking later on.
                if event_id in referenced_events:
                    referenced_events.remove(event.event_id)

        return currently_blocked, referenced_events
