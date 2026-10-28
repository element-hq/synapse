#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

import logging
from typing import TYPE_CHECKING

from synapse.api.constants import EventTypes, HistoryVisibility, JoinRules, Membership
from synapse.api.room_versions import RoomVersion
from synapse.config import ConfigError
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.types import JsonDict, UserID, create_requester
from synapse.types.state import StateFilter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ServerAnnouncementsHandler:
    """Reconcile configured rooms on the main process, without deleting removed entries."""

    def __init__(self, hs: "HomeServer") -> None:
        self._hs = hs
        self._store = hs.get_datastores().main
        self._config = hs.config.serverannouncements
        self._state = hs.get_storage_controllers().state
        self._room_creation = hs.get_room_creation_handler()
        self._room_member = hs.get_room_member_handler()
        self._event_creation = hs.get_event_creation_handler()
        self._creator = self._config.room_management_mxid
        self._requester = create_requester(
            self._creator, authenticated_entity=hs.hostname
        )

    async def validate_configuration(self) -> None:
        """Reject a login-capable management identity before startup completes."""
        if (
            self._config.rooms
            and await self._store.get_user_by_id(self._creator) is not None
        ):
            # Prevent startup if the configured management user already exists.
            raise ConfigError(
                f"The server announcements user {self._creator} already exists; "
                "configure experimental_features.server_announcements.room_management_localpart "
                "with an unused localpart",
                (
                    "experimental_features",
                    "server_announcements",
                    "room_management_localpart",
                ),
            )

    async def reconcile_rooms(self) -> None:
        # This can only happen on the main process.
        if self._hs.config.worker.worker_app or not self._config.rooms:
            return

        await self.validate_configuration()

        for room in self._config.rooms:
            try:
                await self._reconcile_room(room)
            except Exception:
                logger.exception(
                    "Failed to reconcile server announcement room with announcement id '%s'",
                    room.config_id,
                )
                raise

    def _power_levels(
        self, room: ServerAnnouncementRoom, version: RoomVersion
    ) -> JsonDict:
        """The power levels a server announcement room must have."""
        # Announcement-sending users are given PL50.
        #
        # To change state events, you must have PL100.
        # Only the management user may have that.
        users = dict.fromkeys(room.allowed_user_ids, 50)
        if not version.msc4289_creator_power_enabled:
            users[self._creator] = 100

        return {
            "users": users,
            "users_default": 0,
            "events_default": 50,
            "state_default": 100,
            "events": {},
            "invite": 100,
            "kick": 100,
            "ban": 100,
            "redact": 100,
        }

    async def _reconcile_room(self, room: ServerAnnouncementRoom) -> None:
        """Ensure that a given room matches its announcement configuration."""
        # Check that all configured announcement sending users exist.
        for user_id in room.allowed_user_ids:
            user = await self._store.get_user_by_id(user_id)
            if user is None or user.is_deactivated:
                logger.warning(
                    "Announcement sender %s must be an existing, active local user",
                    user_id,
                )

        # Check whether this room already exists.
        room_id = await self._store.get_server_announcement_room(room.config_id)
        if room_id is None:
            # Create the room.
            version = self._hs.config.server.default_room_version
            room_id, _, _ = await self._room_creation.create_room(
                self._requester,
                {
                    "name": room.room_name,
                    "visibility": "private",
                    "preset": "private_chat",
                    "creation_content": {"m.federate": False},
                    "power_level_content_override": self._power_levels(room, version),
                    "initial_state": [
                        {
                            "type": EventTypes.RoomHistoryVisibility,
                            "state_key": "",
                            "content": {
                                # Allow users to read the room without joining.
                                "history_visibility": HistoryVisibility.WORLD_READABLE
                            },
                        },
                        {
                            # Prevent guest joins, not reads of world-readable history.
                            "type": EventTypes.GuestAccess,
                            "state_key": "",
                            "content": {"guest_access": "forbidden"},
                        },
                    ],
                },
                ratelimit=False,
                creator_join_profile={},
                # We definitely don't want these rooms to be encrypted.
                ignore_forced_encryption=True,
            )
            await self._store.store_server_announcement_room(room.config_id, room_id)
            logger.info(
                "Created server announcement room '%s' with matrix room ID '%s'",
                room.config_id,
                room_id,
            )

        # Run checks on the room state.

        create_event = await self._state.get_current_state_event(
            room_id, EventTypes.Create, ""
        )
        if create_event is None:
            raise ConfigError(
                f"Announcement room {room.config_id} ({room_id}) has no create event; "
                "purge the room before reconciling it again",
                ("experimental_features", "server_announcements", "rooms"),
            )

        if create_event.content.get("m.federate", True):
            # We don't want announcement room events to federate to other homeservers.
            # If this room is somehow federating, then we shouldn't use it.
            raise ConfigError(
                f'Announcement room {room.config_id} ({room_id}) has "m.federate": true '
                "in its create event. Please create another room (with a new id)",
                (
                    "experimental_features",
                    "server_announcements",
                    "rooms",
                    room.config_id,
                ),
            )

        # Check that this room was created by the expected management mxid.
        if create_event.sender != self._creator:
            raise ConfigError(
                f"Announcement room {room.config_id} ({room_id}) was created by "
                f"{create_event.sender}, not {self._creator}. Restore "
                "experimental_features.server_announcements.room_management_localpart "
                "or purge the room to recreate it",
                (
                    "experimental_features",
                    "server_announcements",
                    "room_management_localpart",
                ),
            )

        # Update the state of the room if necessary.
        await self._update_state(
            room_id,
            EventTypes.PowerLevels,
            self._power_levels(room, create_event.room_version),
        )
        await self._update_state(
            room_id, EventTypes.JoinRules, {"join_rule": JoinRules.INVITE}
        )
        await self._update_state(
            room_id,
            EventTypes.RoomHistoryVisibility,
            {"history_visibility": HistoryVisibility.WORLD_READABLE},
        )
        await self._update_state(room_id, EventTypes.Name, {"name": room.room_name})
        await self._update_state(
            room_id,
            EventTypes.Topic,
            {"topic": room.room_topic or ""},
        )
        await self._update_state(
            room_id,
            EventTypes.RoomAvatar,
            {"url": room.room_avatar_url} if room.room_avatar_url is not None else {},
        )

        # Revoke both membership and outstanding invitations of any users not in
        # `allowed_user_ids`.
        #
        # The internal creator stays joined regardless.
        members = await self._state.get_current_state(
            room_id, StateFilter.from_types([(EventTypes.Member, None)])
        )
        allowed = set(room.allowed_user_ids)
        for (_, user_id), event in members.items():
            if user_id == self._creator:
                continue

            # Kick the user if they're no longer in the configured list.
            if user_id not in allowed and event.membership in (
                Membership.JOIN,
                Membership.INVITE,
                Membership.KNOCK,
            ):
                await self._room_member.update_membership(
                    self._requester,
                    UserID.from_string(user_id),
                    room_id,
                    Membership.LEAVE,
                    ratelimit=False,
                )

        # Ensure all configured users are joined to the room.
        for user_id in allowed:
            member_event = members.get((EventTypes.Member, user_id))
            membership = member_event.membership if member_event else None
            if membership == Membership.JOIN:
                continue

            # First, unban the user if they happen to be banned.
            target = UserID.from_string(user_id)
            if membership == Membership.BAN:
                await self._room_member.update_membership(
                    self._requester,
                    target,
                    room_id,
                    "unban",
                    ratelimit=False,
                )

            # Invite the user if they don't already have an invitation.
            if membership != Membership.INVITE:
                await self._room_member.update_membership(
                    self._requester,
                    target,
                    room_id,
                    Membership.INVITE,
                    ratelimit=False,
                )

            # Force-join the user.
            await self._room_member.update_membership(
                create_requester(user_id, authenticated_entity=self._hs.hostname),
                target,
                room_id,
                Membership.JOIN,
                ratelimit=False,
            )

    async def _update_state(
        self, room_id: str, event_type: str, content: JsonDict
    ) -> None:
        """Update an announcement room's state only if its content has changed.

        Check the current state first to avoid sending redundant state events.
        """
        previous = await self._state.get_current_state_event(room_id, event_type, "")
        if (previous is None and not content) or (
            previous is not None and previous.content == content
        ):
            return
        await self._event_creation.create_and_send_nonmember_event(
            self._requester,
            {
                "room_id": room_id,
                "sender": self._creator,
                "type": event_type,
                "state_key": "",
                "content": content,
            },
            ratelimit=False,
        )
