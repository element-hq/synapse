#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, List, Mapping, Optional, Union

from synapse import event_auth
from synapse.api.constants import (
    EventTypes,
    JoinRules,
    Membership,
    RestrictedJoinRuleTypes,
)
from synapse.api.errors import AuthError, Codes, SynapseError
from synapse.api.room_versions import RoomVersion
from synapse.event_auth import (
    check_state_dependent_auth_rules,
    check_state_independent_auth_rules,
)
from synapse.events import EventBase
from synapse.events.builder import EventBuilder
from synapse.types import StateMap, StrCollection

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class EventAuthHandler:
    """
    This class contains methods for authenticating events added to room graphs.
    """

    def __init__(self, hs: "HomeServer"):
        self._clock = hs.get_clock()
        self._store = hs.get_datastores().main
        self._state_storage_controller = hs.get_storage_controllers().state
        self._server_name = hs.hostname
        self._is_mine_id = hs.is_mine_id

    async def check_auth_rules_from_context(
        self,
        event: EventBase,
        batched_auth_events: Optional[Mapping[str, EventBase]] = None,
    ) -> None:
        """Check an event passes the auth rules at its own auth events
        Args:
            event: event to be authed
            batched_auth_events: if the event being authed is part of a batch, any events
            from the same batch that may be necessary to auth the current event
        """
        await check_state_independent_auth_rules(
            self._store, event, batched_auth_events
        )
        auth_event_ids = event.auth_event_ids()

        if batched_auth_events:
            # Copy the batched auth events to avoid mutating them.
            auth_events_by_id = dict(batched_auth_events)
            needed_auth_event_ids = set(auth_event_ids) - set(batched_auth_events)
            if needed_auth_event_ids:
                auth_events_by_id.update(
                    await self._store.get_events(needed_auth_event_ids)
                )
        else:
            auth_events_by_id = await self._store.get_events(auth_event_ids)

        check_state_dependent_auth_rules(event, auth_events_by_id.values())

    def compute_auth_events(
        self,
        event: Union[EventBase, EventBuilder],
        current_state_ids: StateMap[str],
        for_verification: bool = False,
    ) -> List[str]:
        """Given an event and current state return the list of event IDs used
        to auth an event.

        If `for_verification` is False then only return auth events that
        should be added to the event's `auth_events`.

        Returns:
            List of event IDs.
        """

        if event.type == EventTypes.Create:
            return []

        # Currently we ignore the `for_verification` flag even though there are
        # some situations where we can drop particular auth events when adding
        # to the event's `auth_events` (e.g. joins pointing to previous joins
        # when room is publicly joinable). Dropping event IDs has the
        # advantage that the auth chain for the room grows slower, but we use
        # the auth chain in state resolution v2 to order events, which means
        # care must be taken if dropping events to ensure that it doesn't
        # introduce undesirable "state reset" behaviour.
        #
        # All of which sounds a bit tricky so we don't bother for now.
        auth_ids = []
        for etype, state_key in event_auth.auth_types_for_event(
            event.room_version, event
        ):
            auth_ev_id = current_state_ids.get((etype, state_key))
            if auth_ev_id:
                auth_ids.append(auth_ev_id)

        return auth_ids

    async def get_user_which_could_invite(
        self, room_id: str, current_state_ids: StateMap[str]
    ) -> str:
        """
        Searches the room state for a local user who has the power level necessary
        to invite other users.

        Args:
            room_id: The room ID under search.
            current_state_ids: The current state of the room.

        Returns:
            The MXID of the user which could issue an invite.

        Raises:
            SynapseError if no appropriate user is found.
        """
        power_level_event_id = current_state_ids.get((EventTypes.PowerLevels, ""))
        invite_level = 0
        users_default_level = 0
        if power_level_event_id:
            power_level_event = await self._store.get_event(power_level_event_id)
            invite_level = power_level_event.content.get("invite", invite_level)
            users_default_level = power_level_event.content.get(
                "users_default", users_default_level
            )
            users = power_level_event.content.get("users", {})
        else:
            users = {}

        # Find the user with the highest power level (only interested in local
        # users).
        local_users_in_room = await self._store.get_local_users_in_room(room_id)
        chosen_user = max(
            local_users_in_room,
            key=lambda user: users.get(user, users_default_level),
            default=None,
        )

        # Return the chosen if they can issue invites.
        user_power_level = users.get(chosen_user, users_default_level)
        if chosen_user and user_power_level >= invite_level:
            logger.debug(
                "Found a user who can issue invites  %s with power level %d >= invite level %d",
                chosen_user,
                user_power_level,
                invite_level,
            )
            return chosen_user

        # No user was found.
        raise SynapseError(
            400,
            "Unable to find a user which could issue an invite",
            Codes.UNABLE_TO_GRANT_JOIN,
        )

    async def is_host_in_room(self, room_id: str, host: str) -> bool:
        return await self._store.is_host_joined(room_id, host)

    async def assert_host_in_room(
        self, room_id: str, host: str, allow_partial_state_rooms: bool = False
    ) -> None:
        """
        Asserts that the host is in the room, or raises an AuthError.

        If the room is partial-stated, we raise an AuthError with the
        UNABLE_DUE_TO_PARTIAL_STATE error code, unless `allow_partial_state_rooms` is true.

        If allow_partial_state_rooms is True and the room is partial-stated,
        this function may return an incorrect result as we are not able to fully
        track server membership in a room without full state.
        """
        if await self._store.is_partial_state_room(room_id):
            if allow_partial_state_rooms:
                current_hosts = await self._state_storage_controller.get_current_hosts_in_room_or_partial_state_approximation(
                    room_id
                )
                if host not in current_hosts:
                    raise AuthError(403, "Host not in room (partial-state approx).")
            else:
                raise AuthError(
                    403,
                    "Unable to authorise you right now; room is partial-stated here.",
                    errcode=Codes.UNABLE_DUE_TO_PARTIAL_STATE,
                )
        else:
            if not await self.is_host_in_room(room_id, host):
                raise AuthError(403, "Host not in room.")

    async def check_restricted_join_rules(
        self,
        state_ids: StateMap[str],
        room_version: RoomVersion,
        user_id: str,
        prev_membership: Optional[str],
    ) -> None:
        """
        Check whether a user can join a room without an invite due to restricted join rules.

        When joining a room with restricted joined rules (as defined in MSC3083),
        the membership of rooms must be checked during a room join.

        Args:
            state_ids: The state of the room as it currently is.
            room_version: The room version of the room being joined.
            user_id: The user joining the room.
            prev_membership: The current membership state for this user. `None` if the
                user has never joined the room (equivalent to "leave").

        Raises:
            AuthError if the user cannot join the room.
        """
        # If the member is invited or currently joined, then nothing to do.
        if prev_membership in (Membership.JOIN, Membership.INVITE):
            return

        # This is not a room with a restricted join rule, so we don't need to do the
        # restricted room specific checks.
        #
        # Note: We'll be applying the standard join rule checks later, which will
        # catch the cases of e.g. trying to join private rooms without an invite.
        if not await self.has_restricted_join_rules(state_ids, room_version):
            return

        # Get the rooms which allow access to this room and check if the user is
        # in any of them.
        allowed_rooms = await self.get_rooms_that_allow_join(state_ids)
        if not await self.is_user_in_rooms(allowed_rooms, user_id):
            # If this is a remote request, the user might be in an allowed room
            # that we do not know about.
            if not self._is_mine_id(user_id):
                for room_id in allowed_rooms:
                    if not await self._store.is_host_joined(room_id, self._server_name):
                        raise SynapseError(
                            400,
                            f"Unable to check if {user_id} is in allowed rooms.",
                            Codes.UNABLE_AUTHORISE_JOIN,
                        )

            raise AuthError(
                403,
                "You do not belong to any of the required rooms/spaces to join this room.",
            )

    async def has_restricted_join_rules(
        self, partial_state_ids: StateMap[str], room_version: RoomVersion
    ) -> bool:
        """
        Return if the room has the proper join rules set for access via rooms.

        Args:
            state_ids: The state of the room as it currently is. May be full or partial
                state.
            room_version: The room version of the room to query.

        Returns:
            True if the proper room version and join rules are set for restricted access.
        """
        # This only applies to room versions which support the new join rule.
        if not room_version.restricted_join_rule:
            return False

        # If there's no join rule, then it defaults to invite (so this doesn't apply).
        join_rules_event_id = partial_state_ids.get((EventTypes.JoinRules, ""), None)
        if not join_rules_event_id:
            return False

        # If the join rule is not restricted, this doesn't apply.
        join_rules_event = await self._store.get_event(join_rules_event_id)
        content_join_rule = join_rules_event.content.get("join_rule")
        if content_join_rule == JoinRules.RESTRICTED:
            return True

        # also check for MSC3787 behaviour
        if room_version.knock_restricted_join_rule:
            return content_join_rule == JoinRules.KNOCK_RESTRICTED

        return False

    async def get_rooms_that_allow_join(
        self, state_ids: StateMap[str]
    ) -> StrCollection:
        """
        Generate a list of rooms in which membership allows access to a room.

        Args:
            state_ids: The current state of the room the user wishes to join

        Returns:
            A collection of room IDs. Membership in any of the rooms in the list grants the ability to join the target room.
        """
        # If there's no join rule, then it defaults to invite (so this doesn't apply).
        join_rules_event_id = state_ids.get((EventTypes.JoinRules, ""), None)
        if not join_rules_event_id:
            return ()

        # If the join rule is not restricted, this doesn't apply.
        join_rules_event = await self._store.get_event(join_rules_event_id)

        # If allowed is of the wrong form, then only allow invited users.
        allow_list = join_rules_event.content.get("allow", [])
        if not isinstance(allow_list, list):
            return ()

        # Pull out the other room IDs, invalid data gets filtered.
        result = []
        for allow in allow_list:
            if not isinstance(allow, dict):
                continue

            # If the type is unexpected, skip it.
            if allow.get("type") != RestrictedJoinRuleTypes.ROOM_MEMBERSHIP:
                continue

            room_id = allow.get("room_id")
            if not isinstance(room_id, str):
                continue

            result.append(room_id)

        return result

    async def is_user_in_rooms(self, room_ids: StrCollection, user_id: str) -> bool:
        """
        Check whether a user is a member of any of the provided rooms.

        Args:
            room_ids: The rooms to check for membership.
            user_id: The user to check.

        Returns:
            True if the user is in any of the rooms, false otherwise.
        """
        if not room_ids:
            return False

        # Get the list of joined rooms and see if there's an overlap.
        joined_rooms = await self._store.get_rooms_for_user(user_id)

        # Check each room and see if the user is in it.
        for room_id in room_ids:
            if room_id in joined_rooms:
                return True

        # The user was not in any of the rooms.
        return False
