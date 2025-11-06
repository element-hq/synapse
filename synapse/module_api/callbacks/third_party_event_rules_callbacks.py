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
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from twisted.internet.defer import CancelledError

from synapse.api.errors import ModuleFailedException, SynapseError
from synapse.events import EventBase
from synapse.events.snapshot import UnpersistedEventContextBase
from synapse.storage.roommember import ProfileInfo
from synapse.types import Requester, StateMap
from synapse.util.async_helpers import delay_cancellation, maybe_awaitable

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


CHECK_EVENT_ALLOWED_CALLBACK = Callable[
    [EventBase, StateMap[EventBase]], Awaitable[tuple[bool, dict | None]]
]
ON_CREATE_ROOM_CALLBACK = Callable[[Requester, dict, bool], Awaitable]
CHECK_THREEPID_CAN_BE_INVITED_CALLBACK = Callable[
    [str, str, StateMap[EventBase]], Awaitable[bool]
]
CHECK_VISIBILITY_CAN_BE_MODIFIED_CALLBACK = Callable[
    [str, StateMap[EventBase], str], Awaitable[bool]
]
ON_NEW_EVENT_CALLBACK = Callable[[EventBase, StateMap[EventBase]], Awaitable]
CHECK_CAN_SHUTDOWN_ROOM_CALLBACK = Callable[[str | None, str], Awaitable[bool]]
CHECK_CAN_DEACTIVATE_USER_CALLBACK = Callable[[str, bool], Awaitable[bool]]
ON_PROFILE_UPDATE_CALLBACK = Callable[[str, ProfileInfo, bool, bool], Awaitable]
ON_USER_DEACTIVATION_STATUS_CHANGED_CALLBACK = Callable[[str, bool, bool], Awaitable]
ON_THREEPID_BIND_CALLBACK = Callable[[str, str, str], Awaitable]
ON_ADD_USER_THIRD_PARTY_IDENTIFIER_CALLBACK = Callable[[str, str, str], Awaitable]
ON_REMOVE_USER_THIRD_PARTY_IDENTIFIER_CALLBACK = Callable[[str, str, str], Awaitable]


def load_legacy_third_party_event_rules(hs: "HomeServer") -> None:
    """Wrapper that loads a third party event rules module configured using the old
    configuration, and registers the hooks they implement.
    """
    if hs.config.thirdpartyrules.third_party_event_rules is None:
        return

    module, config = hs.config.thirdpartyrules.third_party_event_rules

    api = hs.get_module_api()
    third_party_rules = module(config=config, module_api=api)

    # The known hooks. If a module implements a method which name appears in this set,
    # we'll want to register it.
    third_party_event_rules_methods = {
        "check_event_allowed",
        "on_create_room",
        "check_threepid_can_be_invited",
        "check_visibility_can_be_modified",
    }

    def async_wrapper(f: Callable | None) -> Callable[..., Awaitable] | None:
        # f might be None if the callback isn't implemented by the module. In this
        # case we don't want to register a callback at all so we return None.
        if f is None:
            return None

        # We return a separate wrapper for these methods because, in order to wrap them
        # correctly, we need to await its result. Therefore it doesn't make a lot of
        # sense to make it go through the run() wrapper.
        if f.__name__ == "check_event_allowed":
            # We need to wrap check_event_allowed because its old form would return either
            # a boolean or a dict, but now we want to return the dict separately from the
            # boolean.
            async def wrap_check_event_allowed(
                event: EventBase,
                state_events: StateMap[EventBase],
            ) -> tuple[bool, dict | None]:
                # Assertion required because mypy can't prove we won't change
                # `f` back to `None`. See
                # https://mypy.readthedocs.io/en/latest/common_issues.html#narrowing-and-inner-functions
                assert f is not None

                res = await f(event, state_events)
                if isinstance(res, dict):
                    return True, res
                else:
                    return res, None

            return wrap_check_event_allowed

        if f.__name__ == "on_create_room":
            # We need to wrap on_create_room because its old form would return a boolean
            # if the room creation is denied, but now we just want it to raise an
            # exception.
            async def wrap_on_create_room(
                requester: Requester, config: dict, is_requester_admin: bool
            ) -> None:
                # Assertion required because mypy can't prove we won't change
                # `f` back to `None`. See
                # https://mypy.readthedocs.io/en/latest/common_issues.html#narrowing-and-inner-functions
                assert f is not None

                res = await f(requester, config, is_requester_admin)
                if res is False:
                    raise SynapseError(
                        403,
                        "Room creation forbidden with these parameters",
                    )

            return wrap_on_create_room

        def run(*args: Any, **kwargs: Any) -> Awaitable:
            # Assertion required because mypy can't prove we won't change  `f`
            # back to `None`. See
            # https://mypy.readthedocs.io/en/latest/common_issues.html#narrowing-and-inner-functions
            assert f is not None

            return maybe_awaitable(f(*args, **kwargs))

        return run

    # Register the hooks through the module API.
    hooks = {
        hook: async_wrapper(getattr(third_party_rules, hook, None))
        for hook in third_party_event_rules_methods
    }

    api.register_third_party_rules_callbacks(**hooks)


class ThirdPartyEventRulesModuleApiCallbacks:
    """Allows server admins to provide a Python module implementing an extra
    set of rules to apply when processing events.

    This is designed to help admins of closed federations with enforcing custom
    behaviours.
    """

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()

        self._check_event_allowed_callbacks: list[CHECK_EVENT_ALLOWED_CALLBACK] = []
        self._on_create_room_callbacks: list[ON_CREATE_ROOM_CALLBACK] = []
        self._check_threepid_can_be_invited_callbacks: list[
            CHECK_THREEPID_CAN_BE_INVITED_CALLBACK
        ] = []
        self._check_visibility_can_be_modified_callbacks: list[
            CHECK_VISIBILITY_CAN_BE_MODIFIED_CALLBACK
        ] = []
        self._on_new_event_callbacks: list[ON_NEW_EVENT_CALLBACK] = []
        self._check_can_shutdown_room_callbacks: list[
            CHECK_CAN_SHUTDOWN_ROOM_CALLBACK
        ] = []
        self._check_can_deactivate_user_callbacks: list[
            CHECK_CAN_DEACTIVATE_USER_CALLBACK
        ] = []
        self._on_profile_update_callbacks: list[ON_PROFILE_UPDATE_CALLBACK] = []
        self._on_user_deactivation_status_changed_callbacks: list[
            ON_USER_DEACTIVATION_STATUS_CHANGED_CALLBACK
        ] = []
        self._on_threepid_bind_callbacks: list[ON_THREEPID_BIND_CALLBACK] = []
        self._on_add_user_third_party_identifier_callbacks: list[
            ON_ADD_USER_THIRD_PARTY_IDENTIFIER_CALLBACK
        ] = []
        self._on_remove_user_third_party_identifier_callbacks: list[
            ON_REMOVE_USER_THIRD_PARTY_IDENTIFIER_CALLBACK
        ] = []

    def register_third_party_rules_callbacks(
        self,
        check_event_allowed: CHECK_EVENT_ALLOWED_CALLBACK | None = None,
        on_create_room: ON_CREATE_ROOM_CALLBACK | None = None,
        check_threepid_can_be_invited: CHECK_THREEPID_CAN_BE_INVITED_CALLBACK
        | None = None,
        check_visibility_can_be_modified: CHECK_VISIBILITY_CAN_BE_MODIFIED_CALLBACK
        | None = None,
        on_new_event: ON_NEW_EVENT_CALLBACK | None = None,
        check_can_shutdown_room: CHECK_CAN_SHUTDOWN_ROOM_CALLBACK | None = None,
        check_can_deactivate_user: CHECK_CAN_DEACTIVATE_USER_CALLBACK | None = None,
        on_profile_update: ON_PROFILE_UPDATE_CALLBACK | None = None,
        on_user_deactivation_status_changed: ON_USER_DEACTIVATION_STATUS_CHANGED_CALLBACK
        | None = None,
        on_threepid_bind: ON_THREEPID_BIND_CALLBACK | None = None,
        on_add_user_third_party_identifier: ON_ADD_USER_THIRD_PARTY_IDENTIFIER_CALLBACK
        | None = None,
        on_remove_user_third_party_identifier: ON_REMOVE_USER_THIRD_PARTY_IDENTIFIER_CALLBACK
        | None = None,
    ) -> None:
        """Register callbacks from modules for each hook."""
        if check_event_allowed is not None:
            self._check_event_allowed_callbacks.append(check_event_allowed)

        if on_create_room is not None:
            self._on_create_room_callbacks.append(on_create_room)

        if check_threepid_can_be_invited is not None:
            self._check_threepid_can_be_invited_callbacks.append(
                check_threepid_can_be_invited,
            )

        if check_visibility_can_be_modified is not None:
            self._check_visibility_can_be_modified_callbacks.append(
                check_visibility_can_be_modified,
            )

        if on_new_event is not None:
            self._on_new_event_callbacks.append(on_new_event)

        if check_can_shutdown_room is not None:
            self._check_can_shutdown_room_callbacks.append(check_can_shutdown_room)

        if check_can_deactivate_user is not None:
            self._check_can_deactivate_user_callbacks.append(check_can_deactivate_user)
        if on_profile_update is not None:
            self._on_profile_update_callbacks.append(on_profile_update)

        if on_user_deactivation_status_changed is not None:
            self._on_user_deactivation_status_changed_callbacks.append(
                on_user_deactivation_status_changed,
            )

        if on_threepid_bind is not None:
            self._on_threepid_bind_callbacks.append(on_threepid_bind)

        if on_add_user_third_party_identifier is not None:
            self._on_add_user_third_party_identifier_callbacks.append(
                on_add_user_third_party_identifier
            )

        if on_remove_user_third_party_identifier is not None:
            self._on_remove_user_third_party_identifier_callbacks.append(
                on_remove_user_third_party_identifier
            )

    async def check_event_allowed(
        self,
        event: EventBase,
        context: UnpersistedEventContextBase,
    ) -> tuple[bool, dict | None]:
        """Check if a provided event should be allowed in the given context.

        The module can return:
            * True: the event is allowed.
            * False: the event is not allowed, and should be rejected with M_FORBIDDEN.

        If the event is allowed, the module can also return a dictionary to use as a
        replacement for the event.

        Args:
            event: The event to be checked.
            context: The context of the event.

        Returns:
            The result from the ThirdPartyRules module, as above.
        """
        # Bail out early without hitting the store if we don't have any callbacks to run.
        if len(self._check_event_allowed_callbacks) == 0:
            return True, None

        prev_state_ids = await context.get_prev_state_ids()

        # Retrieve the state events from the database.
        events = await self.store.get_events(prev_state_ids.values())
        state_events = {(ev.type, ev.state_key): ev for ev in events.values()}

        # Ensure that the event is frozen, to make sure that the module is not tempted
        # to try to modify it. Any attempt to modify it at this point will invalidate
        # the hashes and signatures.
        event.freeze()

        for callback in self._check_event_allowed_callbacks:
            try:
                res, replacement_data = await delay_cancellation(
                    callback(event, state_events)
                )
            except CancelledError:
                raise
            except SynapseError as e:
                # FIXME: Being able to throw SynapseErrors is relied upon by
                # some modules. PR https://github.com/matrix-org/synapse/pull/10386
                # accidentally broke this ability.
                # That said, we aren't keen on exposing this implementation detail
                # to modules and we should one day have a proper way to do what
                # is wanted.
                # This module callback needs a rework so that hacks such as
                # this one are not necessary.
                raise e
            except Exception:
                raise ModuleFailedException(
                    "Failed to run `check_event_allowed` module API callback"
                )

            # Return if the event shouldn't be allowed or if the module came up with a
            # replacement dict for the event.
            if res is False:
                return res, None
            elif isinstance(replacement_data, dict):
                return True, replacement_data

        return True, None

    async def on_create_room(
        self, requester: Requester, config: dict, is_requester_admin: bool
    ) -> None:
        """Intercept requests to create room to maybe deny it (via an exception) or
        update the request config.

        Args:
            requester
            config: The creation config from the client.
            is_requester_admin: If the requester is an admin
        """
        for callback in self._on_create_room_callbacks:
            try:
                await callback(requester, config, is_requester_admin)
            except Exception as e:
                # Don't silence the errors raised by this callback since we expect it to
                # raise an exception to deny the creation of the room; instead make sure
                # it's a SynapseError we can send to clients.
                if not isinstance(e, SynapseError):
                    e = SynapseError(
                        403, "Room creation forbidden with these parameters"
                    )

                raise e

    async def check_threepid_can_be_invited(
        self, medium: str, address: str, room_id: str
    ) -> bool:
        """Check if a provided 3PID can be invited in the given room.

        Args:
            medium: The 3PID's medium.
            address: The 3PID's address.
            room_id: The room we want to invite the threepid to.

        Returns:
            True if the 3PID can be invited, False if not.
        """
        # Bail out early without hitting the store if we don't have any callbacks to run.
        if len(self._check_threepid_can_be_invited_callbacks) == 0:
            return True

        state_events = await self._storage_controllers.state.get_current_state(room_id)

        for callback in self._check_threepid_can_be_invited_callbacks:
            try:
                threepid_can_be_invited = await delay_cancellation(
                    callback(medium, address, state_events)
                )
                if threepid_can_be_invited is False:
                    return False
            except CancelledError:
                raise
            except Exception as e:
                logger.warning("Failed to run module API callback %s: %s", callback, e)

        return True

    async def check_visibility_can_be_modified(
        self, room_id: str, new_visibility: str
    ) -> bool:
        """Check if a room is allowed to be published to, or removed from, the public room
        list.

        Args:
            room_id: The ID of the room.
            new_visibility: The new visibility state. Either "public" or "private".

        Returns:
            True if the room's visibility can be modified, False if not.
        """
        # Bail out early without hitting the store if we don't have any callback
        if len(self._check_visibility_can_be_modified_callbacks) == 0:
            return True

        state_events = await self._storage_controllers.state.get_current_state(room_id)

        for callback in self._check_visibility_can_be_modified_callbacks:
            try:
                visibility_can_be_modified = await delay_cancellation(
                    callback(room_id, state_events, new_visibility)
                )
                if visibility_can_be_modified is False:
                    return False
            except CancelledError:
                raise
            except Exception as e:
                logger.warning("Failed to run module API callback %s: %s", callback, e)

        return True

    async def on_new_event(self, event_id: str) -> None:
        """Let modules act on events after they've been sent (e.g. auto-accepting
        invites, etc.)

        Args:
            event_id: The ID of the event.
        """
        # Bail out early without hitting the store if we don't have any callbacks
        if len(self._on_new_event_callbacks) == 0:
            return

        event = await self.store.get_event(event_id)

        # We *don't* want to wait for the full state here, because waiting for full
        # state will persist event, which in turn will call this method.
        # This would end up in a deadlock.
        state_events = await self._storage_controllers.state.get_current_state(
            event.room_id, await_full_state=False
        )

        for callback in self._on_new_event_callbacks:
            try:
                await callback(event, state_events)
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )

    async def check_can_shutdown_room(self, user_id: str | None, room_id: str) -> bool:
        """Intercept requests to shutdown a room. If `False` is returned, the
         room must not be shut down.

        Args:
            user_id: The ID of the user requesting the shutdown.
                If no user ID is supplied, then the room is being shut down through
                some mechanism other than a user's request, e.g. through a module's
                request.
            room_id: The ID of the room.
        """
        for callback in self._check_can_shutdown_room_callbacks:
            try:
                can_shutdown_room = await delay_cancellation(callback(user_id, room_id))
                if can_shutdown_room is False:
                    return False
            except CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )
        return True

    async def check_can_deactivate_user(
        self,
        user_id: str,
        by_admin: bool,
    ) -> bool:
        """Intercept requests to deactivate a user. If `False` is returned, the
        user should not be deactivated.

        Args:
            requester
            user_id: The ID of the room.
        """
        for callback in self._check_can_deactivate_user_callbacks:
            try:
                can_deactivate_user = await delay_cancellation(
                    callback(user_id, by_admin)
                )
                if can_deactivate_user is False:
                    return False
            except CancelledError:
                raise
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )
        return True

    async def on_profile_update(
        self, user_id: str, new_profile: ProfileInfo, by_admin: bool, deactivation: bool
    ) -> None:
        """Called after the global profile of a user has been updated. Does not include
        per-room profile changes.

        Args:
            user_id: The user whose profile was changed.
            new_profile: The updated profile for the user.
            by_admin: Whether the profile update was performed by a server admin.
            deactivation: Whether this change was made while deactivating the user.
        """
        for callback in self._on_profile_update_callbacks:
            try:
                await callback(user_id, new_profile, by_admin, deactivation)
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )

    async def on_user_deactivation_status_changed(
        self, user_id: str, deactivated: bool, by_admin: bool
    ) -> None:
        """Called after a user has been deactivated or reactivated.

        Args:
            user_id: The deactivated user.
            deactivated: Whether the user is now deactivated.
            by_admin: Whether the deactivation was performed by a server admin.
        """
        for callback in self._on_user_deactivation_status_changed_callbacks:
            try:
                await callback(user_id, deactivated, by_admin)
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )

    async def on_threepid_bind(self, user_id: str, medium: str, address: str) -> None:
        """Called after a threepid association has been verified and stored.

        Note that this callback is called when an association is created on the
        local homeserver, not when it's created on an identity server (and then kept track
        of so that it can be unbound on the same IS later on).

        THIS MODULE CALLBACK METHOD HAS BEEN DEPRECATED. Please use the
        `on_add_user_third_party_identifier` callback method instead.

        Args:
            user_id: the user being associated with the threepid.
            medium: the threepid's medium.
            address: the threepid's address.
        """
        for callback in self._on_threepid_bind_callbacks:
            try:
                await callback(user_id, medium, address)
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )

    async def on_add_user_third_party_identifier(
        self, user_id: str, medium: str, address: str
    ) -> None:
        """Called when an association between a user's Matrix ID and a third-party ID
        (email, phone number) has successfully been registered on the homeserver.

        Args:
            user_id: The User ID included in the association.
            medium: The medium of the third-party ID (email, msisdn).
            address: The address of the third-party ID (i.e. an email address).
        """
        for callback in self._on_add_user_third_party_identifier_callbacks:
            try:
                await callback(user_id, medium, address)
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )

    async def on_remove_user_third_party_identifier(
        self, user_id: str, medium: str, address: str
    ) -> None:
        """Called when an association between a user's Matrix ID and a third-party ID
        (email, phone number) has been successfully removed on the homeserver.

        This is called *after* any known bindings on identity servers for this
        association have been removed.

        Args:
            user_id: The User ID included in the removed association.
            medium: The medium of the third-party ID (email, msisdn).
            address: The address of the third-party ID (i.e. an email address).
        """
        for callback in self._on_remove_user_third_party_identifier_callbacks:
            try:
                await callback(user_id, medium, address)
            except Exception as e:
                logger.exception(
                    "Failed to run module API callback %s: %s", callback, e
                )
