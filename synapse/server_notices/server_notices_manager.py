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
from typing import TYPE_CHECKING, Optional

from synapse.api.constants import EventTypes, Membership, RoomCreationPreset
from synapse.events import EventBase
from synapse.types import JsonDict, Requester, StreamKeyType, UserID, create_requester
from synapse.util.caches.descriptors import cached

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

SERVER_NOTICE_ROOM_TAG = "m.server_notice"


class ServerNoticesManager:
    def __init__(self, hs: "HomeServer"):
        self.server_name = hs.hostname  # nb must be called this for @cached
        self._store = hs.get_datastores().main
        self._config = hs.config
        self._account_data_handler = hs.get_account_data_handler()
        self._room_creation_handler = hs.get_room_creation_handler()
        self._room_member_handler = hs.get_room_member_handler()
        self._event_creation_handler = hs.get_event_creation_handler()
        self._message_handler = hs.get_message_handler()
        self._storage_controllers = hs.get_storage_controllers()
        self._is_mine_id = hs.is_mine_id

        self._notifier = hs.get_notifier()
        self.server_notices_mxid = self._config.servernotices.server_notices_mxid

    def is_enabled(self) -> bool:
        """Checks if server notices are enabled on this server."""
        return self.server_notices_mxid is not None

    async def send_notice(
        self,
        user_id: str,
        event_content: dict,
        type: str = EventTypes.Message,
        state_key: Optional[str] = None,
        txn_id: Optional[str] = None,
    ) -> EventBase:
        """Send a notice to the given user

        Creates the server notices room, if none exists.

        Args:
            user_id: mxid of user to send event to.
            event_content: content of event to send
            type: type of event
            is_state_event: Is the event a state event
            txn_id: The transaction ID.
        """
        room_id = await self.get_or_create_notice_room_for_user(user_id)
        await self.maybe_invite_user_to_room(user_id, room_id)

        assert self.server_notices_mxid is not None
        requester = create_requester(
            self.server_notices_mxid, authenticated_entity=self.server_name
        )

        logger.info("Sending server notice to %s", user_id)

        event_dict = {
            "type": type,
            "room_id": room_id,
            "sender": self.server_notices_mxid,
            "content": event_content,
        }

        if state_key is not None:
            event_dict["state_key"] = state_key

        event, _ = await self._event_creation_handler.create_and_send_nonmember_event(
            requester, event_dict, ratelimit=False, txn_id=txn_id
        )
        return event

    @cached()
    async def maybe_get_notice_room_for_user(self, user_id: str) -> Optional[str]:
        """Try to look up the server notice room for this user if it exists.

        Does not create one if none can be found.

        Args:
            user_id: the user we want a server notice room for.

        Returns:
            The room's ID, or None if no room could be found.
        """
        # If there is no server notices MXID, then there is no server notices room
        if self.server_notices_mxid is None:
            return None

        rooms = await self._store.get_rooms_for_local_user_where_membership_is(
            user_id, [Membership.INVITE, Membership.JOIN]
        )
        for room in rooms:
            # it's worth noting that there is an asymmetry here in that we
            # expect the user to be invited or joined, but the system user must
            # be joined. This is kinda deliberate, in that if somebody somehow
            # manages to invite the system user to a room, that doesn't make it
            # the server notices room.
            is_server_notices_room = await self._store.check_local_user_in_room(
                user_id=self.server_notices_mxid, room_id=room.room_id
            )
            if is_server_notices_room:
                # we found a room which our user shares with the system notice
                # user
                return room.room_id

        return None

    @cached()
    async def get_or_create_notice_room_for_user(self, user_id: str) -> str:
        """Get the room for notices for a given user

        If we have not yet created a notice room for this user, create it, but don't
        invite the user to it.

        Args:
            user_id: complete user id for the user we want a room for

        Returns:
            room id of notice room.
        """
        if self.server_notices_mxid is None:
            raise Exception("Server notices not enabled")

        assert self._is_mine_id(user_id), "Cannot send server notices to remote users"

        requester = create_requester(
            self.server_notices_mxid, authenticated_entity=self.server_name
        )

        room_id = await self.maybe_get_notice_room_for_user(user_id)
        if room_id is not None:
            logger.info(
                "Using existing server notices room %s for user %s",
                room_id,
                user_id,
            )
            await self._update_notice_user_profile_if_changed(
                requester,
                room_id,
                self._config.servernotices.server_notices_mxid_display_name,
                self._config.servernotices.server_notices_mxid_avatar_url,
            )
            await self._update_room_info(
                requester,
                room_id,
                EventTypes.Name,
                "name",
                self._config.servernotices.server_notices_room_name,
            )
            await self._update_room_info(
                requester,
                room_id,
                EventTypes.RoomAvatar,
                "url",
                self._config.servernotices.server_notices_room_avatar_url,
            )
            await self._update_room_info(
                requester,
                room_id,
                EventTypes.Topic,
                "topic",
                self._config.servernotices.server_notices_room_topic,
            )
            return room_id

        # apparently no existing notice room: create a new one
        logger.info("Creating server notices room for %s", user_id)

        # see if we want to override the profile info for the server user.
        # note that if we want to override either the display name or the
        # avatar, we have to use both.
        join_profile = None
        if (
            self._config.servernotices.server_notices_mxid_display_name is not None
            or self._config.servernotices.server_notices_mxid_avatar_url is not None
        ):
            join_profile = {
                "displayname": self._config.servernotices.server_notices_mxid_display_name,
                "avatar_url": self._config.servernotices.server_notices_mxid_avatar_url,
            }

        room_config: JsonDict = {
            "preset": RoomCreationPreset.PRIVATE_CHAT,
            "power_level_content_override": {"users_default": -10},
        }

        if self._config.servernotices.server_notices_room_name:
            room_config["name"] = self._config.servernotices.server_notices_room_name
        if self._config.servernotices.server_notices_room_topic:
            room_config["topic"] = self._config.servernotices.server_notices_room_topic
        if self._config.servernotices.server_notices_room_avatar_url:
            room_config["initial_state"] = [
                {
                    "type": EventTypes.RoomAvatar,
                    "state_key": "",
                    "content": {
                        "url": self._config.servernotices.server_notices_room_avatar_url,
                    },
                }
            ]

        # `ignore_forced_encryption` is used to bypass `encryption_enabled_by_default_for_room_type`
        # setting if it set, since the server notices will not be encrypted anyway.
        room_id, _, _ = await self._room_creation_handler.create_room(
            requester,
            config=room_config,
            ratelimit=False,
            creator_join_profile=join_profile,
            ignore_forced_encryption=True,
        )

        self.maybe_get_notice_room_for_user.invalidate((user_id,))

        max_id = await self._account_data_handler.add_tag_to_room(
            user_id, room_id, SERVER_NOTICE_ROOM_TAG, {}
        )
        self._notifier.on_new_event(StreamKeyType.ACCOUNT_DATA, max_id, users=[user_id])

        logger.info("Created server notices room %s for %s", room_id, user_id)
        return room_id

    async def maybe_invite_user_to_room(self, user_id: str, room_id: str) -> None:
        """Invite the given user to the given server room, unless the user has already
        joined or been invited to it.

        Args:
            user_id: The ID of the user to invite.
            room_id: The ID of the room to invite the user to.
        """
        assert self.server_notices_mxid is not None
        requester = create_requester(
            self.server_notices_mxid, authenticated_entity=self.server_name
        )

        # Check whether the user has already joined or been invited to this room. If
        # that's the case, there is no need to re-invite them.
        joined_rooms = await self._store.get_rooms_for_local_user_where_membership_is(
            user_id, [Membership.INVITE, Membership.JOIN]
        )
        for room in joined_rooms:
            if room.room_id == room_id:
                return

        user_id_obj = UserID.from_string(user_id)
        await self._room_member_handler.update_membership(
            requester=requester,
            target=user_id_obj,
            room_id=room_id,
            action="invite",
            ratelimit=False,
        )

        if self._config.servernotices.server_notices_auto_join:
            user_requester = create_requester(
                user_id, authenticated_entity=self.server_name
            )
            await self._room_member_handler.update_membership(
                requester=user_requester,
                target=user_id_obj,
                room_id=room_id,
                action="join",
                ratelimit=False,
            )

    async def _update_notice_user_profile_if_changed(
        self,
        requester: Requester,
        room_id: str,
        display_name: Optional[str],
        avatar_url: Optional[str],
    ) -> None:
        """
        Updates the notice user's profile if it's different from what is in the room.

        Args:
            requester: The user who is performing the update.
            room_id: The ID of the server notice room
            display_name: The displayname of the server notice user
            avatar_url: The avatar url of the server notice user
        """
        logger.debug("Checking whether notice user profile has changed for %s", room_id)

        assert self.server_notices_mxid is not None

        notice_user_data_in_room = (
            await self._storage_controllers.state.get_current_state_event(
                room_id,
                EventTypes.Member,
                self.server_notices_mxid,
            )
        )

        assert notice_user_data_in_room is not None

        notice_user_profile_changed = (
            display_name != notice_user_data_in_room.content.get("displayname")
            or avatar_url != notice_user_data_in_room.content.get("avatar_url")
        )
        if notice_user_profile_changed:
            logger.info("Updating notice user profile in room %s", room_id)
            await self._room_member_handler.update_membership(
                requester=requester,
                target=UserID.from_string(self.server_notices_mxid),
                room_id=room_id,
                action="join",
                ratelimit=False,
                content={"displayname": display_name, "avatar_url": avatar_url},
            )

    async def _update_room_info(
        self,
        requester: Requester,
        room_id: str,
        info_event_type: str,
        info_content_key: str,
        info_value: Optional[str],
    ) -> None:
        """
        Updates a specific notice room's info if it's different from what is set.

        Args:
            requester: The user who is performing the update.
            room_id: The ID of the server notice room
            info_event_type: The event type holding the specific info
            info_content_key: The key containing the specific info in the event's content
            info_value: The expected value for the specific info
        """
        room_info_event = await self._storage_controllers.state.get_current_state_event(
            room_id,
            info_event_type,
            "",
        )

        existing_info_value = None
        if room_info_event:
            existing_info_value = room_info_event.get(info_content_key)
        if existing_info_value == info_value:
            return
        if not existing_info_value and not info_value:
            # A missing `info_value` can either be represented by a None
            # or an empty string, so we assume that if they're both falsey
            # they're equivalent.
            return

        if info_value is None:
            info_value = ""

        room_info_event_dict = {
            "type": info_event_type,
            "room_id": room_id,
            "sender": requester.user.to_string(),
            "state_key": "",
            "content": {
                info_content_key: info_value,
            },
        }

        event, _ = await self._event_creation_handler.create_and_send_nonmember_event(
            requester, room_info_event_dict, ratelimit=False
        )
