#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016-2021 The Matrix.org Foundation C.I.C.
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

"""Contains functions for performing actions on rooms."""
import itertools
import logging
import math
import random
import string
from collections import OrderedDict
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    cast,
)

import attr

import synapse.events.snapshot
from synapse.api.constants import (
    Direction,
    EventContentFields,
    EventTypes,
    GuestAccess,
    HistoryVisibility,
    JoinRules,
    Membership,
    RoomCreationPreset,
    RoomEncryptionAlgorithms,
    RoomTypes,
)
from synapse.api.errors import (
    AuthError,
    Codes,
    LimitExceededError,
    NotFoundError,
    PartialStateConflictError,
    StoreError,
    SynapseError,
)
from synapse.api.filtering import Filter
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersion
from synapse.event_auth import validate_event_for_room_version
from synapse.events import EventBase
from synapse.events.snapshot import UnpersistedEventContext
from synapse.events.utils import copy_and_fixup_power_levels_contents
from synapse.handlers.relations import BundledAggregations
from synapse.rest.admin._base import assert_user_is_admin
from synapse.streams import EventSource
from synapse.types import (
    JsonDict,
    JsonMapping,
    MutableStateMap,
    Requester,
    RoomAlias,
    RoomID,
    RoomStreamToken,
    StateMap,
    StrCollection,
    StreamKeyType,
    StreamToken,
    UserID,
    create_requester,
)
from synapse.types.handlers import ShutdownRoomParams, ShutdownRoomResponse
from synapse.types.state import StateFilter
from synapse.util import stringutils
from synapse.util.caches.response_cache import ResponseCache
from synapse.util.stringutils import parse_and_validate_server_name
from synapse.visibility import filter_events_for_client

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

id_server_scheme = "https://"

FIVE_MINUTES_IN_MS = 5 * 60 * 1000


@attr.s(slots=True, frozen=True, auto_attribs=True)
class EventContext:
    events_before: List[EventBase]
    event: EventBase
    events_after: List[EventBase]
    state: List[EventBase]
    aggregations: Dict[str, BundledAggregations]
    start: str
    end: str


class RoomCreationHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self.auth = hs.get_auth()
        self.auth_blocking = hs.get_auth_blocking()
        self.clock = hs.get_clock()
        self.hs = hs
        self._spam_checker_module_callbacks = hs.get_module_api_callbacks().spam_checker
        self.event_creation_handler = hs.get_event_creation_handler()
        self.room_member_handler = hs.get_room_member_handler()
        self._event_auth_handler = hs.get_event_auth_handler()
        self.config = hs.config
        self.request_ratelimiter = hs.get_request_ratelimiter()

        # Room state based off defined presets
        self._presets_dict: Dict[str, Dict[str, Any]] = {
            RoomCreationPreset.PRIVATE_CHAT: {
                "join_rules": JoinRules.INVITE,
                "history_visibility": HistoryVisibility.SHARED,
                "original_invitees_have_ops": False,
                "guest_can_join": True,
                "power_level_content_override": {"invite": 0},
            },
            RoomCreationPreset.TRUSTED_PRIVATE_CHAT: {
                "join_rules": JoinRules.INVITE,
                "history_visibility": HistoryVisibility.SHARED,
                "original_invitees_have_ops": True,
                "guest_can_join": True,
                "power_level_content_override": {"invite": 0},
            },
            RoomCreationPreset.PUBLIC_CHAT: {
                "join_rules": JoinRules.PUBLIC,
                "history_visibility": HistoryVisibility.SHARED,
                "original_invitees_have_ops": False,
                "guest_can_join": False,
                "power_level_content_override": {EventTypes.CallInvite: 50},
            },
        }

        # Modify presets to selectively enable encryption by default per homeserver config
        for preset_name, preset_config in self._presets_dict.items():
            encrypted = (
                preset_name
                in self.config.room.encryption_enabled_by_default_for_room_presets
            )
            preset_config["encrypted"] = encrypted

        self._default_power_level_content_override = (
            self.config.room.default_power_level_content_override
        )

        self._replication = hs.get_replication_data_handler()

        # If a user tries to update the same room multiple times in quick
        # succession, only process the first attempt and return its result to
        # subsequent requests
        self._upgrade_response_cache: ResponseCache[Tuple[str, str]] = ResponseCache(
            hs.get_clock(), "room_upgrade", timeout_ms=FIVE_MINUTES_IN_MS
        )
        self._server_notices_mxid = hs.config.servernotices.server_notices_mxid

        self._third_party_event_rules = (
            hs.get_module_api_callbacks().third_party_event_rules
        )

    async def upgrade_room(
        self, requester: Requester, old_room_id: str, new_version: RoomVersion
    ) -> str:
        """Replace a room with a new room with a different version

        Args:
            requester: the user requesting the upgrade
            old_room_id: the id of the room to be replaced
            new_version: the new room version to use

        Returns:
            the new room id

        Raises:
            ShadowBanError if the requester is shadow-banned.
        """
        await self.request_ratelimiter.ratelimit(requester)

        user_id = requester.user.to_string()

        # Check if this room is already being upgraded by another person
        for key in self._upgrade_response_cache.keys():
            if key[0] == old_room_id and key[1] != user_id:
                # Two different people are trying to upgrade the same room.
                # Send the second an error.
                #
                # Note that this of course only gets caught if both users are
                # on the same homeserver.
                raise SynapseError(
                    400, "An upgrade for this room is currently in progress"
                )

        # Check whether the room exists and 404 if it doesn't.
        # We could go straight for the auth check, but that will raise a 403 instead.
        old_room = await self.store.get_room(old_room_id)
        if old_room is None:
            raise NotFoundError("Unknown room id %s" % (old_room_id,))

        new_room_id = self._generate_room_id()

        # Try several times, it could fail with PartialStateConflictError
        # in _upgrade_room, cf comment in except block.
        max_retries = 5
        for i in range(max_retries):
            try:
                # Check whether the user has the power level to carry out the upgrade.
                # `check_auth_rules_from_context` will check that they are in the room and have
                # the required power level to send the tombstone event.
                (
                    tombstone_event,
                    tombstone_unpersisted_context,
                ) = await self.event_creation_handler.create_event(
                    requester,
                    {
                        "type": EventTypes.Tombstone,
                        "state_key": "",
                        "room_id": old_room_id,
                        "sender": user_id,
                        "content": {
                            "body": "This room has been replaced",
                            "replacement_room": new_room_id,
                        },
                    },
                )
                tombstone_context = await tombstone_unpersisted_context.persist(
                    tombstone_event
                )
                validate_event_for_room_version(tombstone_event)
                await self._event_auth_handler.check_auth_rules_from_context(
                    tombstone_event
                )

                # Upgrade the room
                #
                # If this user has sent multiple upgrade requests for the same room
                # and one of them is not complete yet, cache the response and
                # return it to all subsequent requests
                ret = await self._upgrade_response_cache.wrap(
                    (old_room_id, user_id),
                    self._upgrade_room,
                    requester,
                    old_room_id,
                    old_room,  # args for _upgrade_room
                    new_room_id,
                    new_version,
                    tombstone_event,
                    tombstone_context,
                )

                return ret
            except PartialStateConflictError as e:
                # Clean up the cache so we can retry properly
                self._upgrade_response_cache.unset((old_room_id, user_id))
                # Persisting couldn't happen because the room got un-partial stated
                # in the meantime and context needs to be recomputed, so let's do so.
                if i == max_retries - 1:
                    raise e

        # This is to satisfy mypy and should never happen
        raise PartialStateConflictError()

    async def _upgrade_room(
        self,
        requester: Requester,
        old_room_id: str,
        old_room: Tuple[bool, str, bool],
        new_room_id: str,
        new_version: RoomVersion,
        tombstone_event: EventBase,
        tombstone_context: synapse.events.snapshot.EventContext,
    ) -> str:
        """
        Args:
            requester: the user requesting the upgrade
            old_room_id: the id of the room to be replaced
            old_room: a tuple containing room information for the room to be replaced,
                as returned by `RoomWorkerStore.get_room`.
            new_room_id: the id of the replacement room
            new_version: the version to upgrade the room to
            tombstone_event: the tombstone event to send to the old room
            tombstone_context: the context for the tombstone event

        Raises:
            ShadowBanError if the requester is shadow-banned.
        """
        user_id = requester.user.to_string()
        assert self.hs.is_mine_id(user_id), "User must be our own: %s" % (user_id,)

        logger.info("Creating new room %s to replace %s", new_room_id, old_room_id)

        # create the new room. may raise a `StoreError` in the exceedingly unlikely
        # event of a room ID collision.
        await self.store.store_room(
            room_id=new_room_id,
            room_creator_user_id=user_id,
            is_public=old_room[0],
            room_version=new_version,
        )

        await self.clone_existing_room(
            requester,
            old_room_id=old_room_id,
            new_room_id=new_room_id,
            new_room_version=new_version,
            tombstone_event_id=tombstone_event.event_id,
        )

        # now send the tombstone
        await self.event_creation_handler.handle_new_client_event(
            requester=requester,
            events_and_context=[(tombstone_event, tombstone_context)],
        )

        state_filter = StateFilter.from_types(
            [(EventTypes.CanonicalAlias, ""), (EventTypes.PowerLevels, "")]
        )
        old_room_state = await tombstone_context.get_current_state_ids(state_filter)

        # We know the tombstone event isn't an outlier so it has current state.
        assert old_room_state is not None

        # update any aliases
        await self._move_aliases_to_new_room(
            requester, old_room_id, new_room_id, old_room_state
        )

        # Copy over user push rules, tags and migrate room directory state
        await self.room_member_handler.transfer_room_state_on_room_upgrade(
            old_room_id, new_room_id
        )

        # finally, shut down the PLs in the old room, and update them in the new
        # room.
        await self._update_upgraded_room_pls(
            requester,
            old_room_id,
            new_room_id,
            old_room_state,
        )

        return new_room_id

    async def _update_upgraded_room_pls(
        self,
        requester: Requester,
        old_room_id: str,
        new_room_id: str,
        old_room_state: StateMap[str],
    ) -> None:
        """Send updated power levels in both rooms after an upgrade

        Args:
            requester: the user requesting the upgrade
            old_room_id: the id of the room to be replaced
            new_room_id: the id of the replacement room
            old_room_state: the state map for the old room

        Raises:
            ShadowBanError if the requester is shadow-banned.
        """
        old_room_pl_event_id = old_room_state.get((EventTypes.PowerLevels, ""))

        if old_room_pl_event_id is None:
            logger.warning(
                "Not supported: upgrading a room with no PL event. Not setting PLs "
                "in old room."
            )
            return

        old_room_pl_state = await self.store.get_event(old_room_pl_event_id)

        # we try to stop regular users from speaking by setting the PL required
        # to send regular events and invites to 'Moderator' level. That's normally
        # 50, but if the default PL in a room is 50 or more, then we set the
        # required PL above that.

        pl_content = copy_and_fixup_power_levels_contents(old_room_pl_state.content)
        users_default: int = pl_content.get("users_default", 0)  # type: ignore[assignment]
        restricted_level = max(users_default + 1, 50)

        updated = False
        for v in ("invite", "events_default"):
            current: int = pl_content.get(v, 0)  # type: ignore[assignment]
            if current < restricted_level:
                logger.debug(
                    "Setting level for %s in %s to %i (was %i)",
                    v,
                    old_room_id,
                    restricted_level,
                    current,
                )
                pl_content[v] = restricted_level
                updated = True
            else:
                logger.debug("Not setting level for %s (already %i)", v, current)

        if updated:
            try:
                await self.event_creation_handler.create_and_send_nonmember_event(
                    requester,
                    {
                        "type": EventTypes.PowerLevels,
                        "state_key": "",
                        "room_id": old_room_id,
                        "sender": requester.user.to_string(),
                        "content": pl_content,
                    },
                    ratelimit=False,
                )
            except AuthError as e:
                logger.warning("Unable to update PLs in old room: %s", e)

        await self.event_creation_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": EventTypes.PowerLevels,
                "state_key": "",
                "room_id": new_room_id,
                "sender": requester.user.to_string(),
                "content": copy_and_fixup_power_levels_contents(
                    old_room_pl_state.content
                ),
            },
            ratelimit=False,
        )

    async def clone_existing_room(
        self,
        requester: Requester,
        old_room_id: str,
        new_room_id: str,
        new_room_version: RoomVersion,
        tombstone_event_id: str,
    ) -> None:
        """Populate a new room based on an old room

        Args:
            requester: the user requesting the upgrade
            old_room_id : the id of the room to be replaced
            new_room_id: the id to give the new room (should already have been
                created with _generate_room_id())
            new_room_version: the new room version to use
            tombstone_event_id: the ID of the tombstone event in the old room.
        """
        user_id = requester.user.to_string()

        spam_check = await self._spam_checker_module_callbacks.user_may_create_room(
            user_id
        )
        if spam_check != self._spam_checker_module_callbacks.NOT_SPAM:
            raise SynapseError(
                403,
                "You are not permitted to create rooms",
                errcode=spam_check[0],
                additional_fields=spam_check[1],
            )

        creation_content: JsonDict = {
            "room_version": new_room_version.identifier,
            "predecessor": {"room_id": old_room_id, "event_id": tombstone_event_id},
        }

        # Check if old room was non-federatable

        # Get old room's create event
        old_room_create_event = await self.store.get_create_event_for_room(old_room_id)

        # Check if the create event specified a non-federatable room
        if not old_room_create_event.content.get(EventContentFields.FEDERATE, True):
            # If so, mark the new room as non-federatable as well
            creation_content[EventContentFields.FEDERATE] = False

        initial_state = {}

        # Replicate relevant room events
        types_to_copy: List[Tuple[str, Optional[str]]] = [
            (EventTypes.JoinRules, ""),
            (EventTypes.Name, ""),
            (EventTypes.Topic, ""),
            (EventTypes.RoomHistoryVisibility, ""),
            (EventTypes.GuestAccess, ""),
            (EventTypes.RoomAvatar, ""),
            (EventTypes.RoomEncryption, ""),
            (EventTypes.ServerACL, ""),
            (EventTypes.PowerLevels, ""),
        ]

        # Copy the room type as per MSC3818.
        room_type = old_room_create_event.content.get(EventContentFields.ROOM_TYPE)
        if room_type is not None:
            creation_content[EventContentFields.ROOM_TYPE] = room_type

            # If the old room was a space, copy over the rooms in the space.
            if room_type == RoomTypes.SPACE:
                types_to_copy.append((EventTypes.SpaceChild, None))

        old_room_state_ids = (
            await self._storage_controllers.state.get_current_state_ids(
                old_room_id, StateFilter.from_types(types_to_copy)
            )
        )
        # map from event_id to BaseEvent
        old_room_state_events = await self.store.get_events(old_room_state_ids.values())

        for k, old_event_id in old_room_state_ids.items():
            old_event = old_room_state_events.get(old_event_id)
            if old_event:
                # If the event is an space child event with empty content, it was
                # removed from the space and should be ignored.
                if k[0] == EventTypes.SpaceChild and not old_event.content:
                    continue

                initial_state[k] = old_event.content

        # deep-copy the power-levels event before we start modifying it
        # note that if frozen_dicts are enabled, `power_levels` will be a frozen
        # dict so we can't just copy.deepcopy it.
        initial_state[(EventTypes.PowerLevels, "")] = power_levels = (
            copy_and_fixup_power_levels_contents(
                initial_state[(EventTypes.PowerLevels, "")]
            )
        )

        # Resolve the minimum power level required to send any state event
        # We will give the upgrading user this power level temporarily (if necessary) such that
        # they are able to copy all of the state events over, then revert them back to their
        # original power level afterwards in _update_upgraded_room_pls

        # Copy over user power levels now as this will not be possible with >100PL users once
        # the room has been created
        # Calculate the minimum power level needed to clone the room
        event_power_levels = power_levels.get("events", {})
        if not isinstance(event_power_levels, dict):
            event_power_levels = {}
        state_default = power_levels.get("state_default", 50)
        try:
            state_default_int = int(state_default)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            state_default_int = 50
        ban = power_levels.get("ban", 50)
        try:
            ban = int(ban)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            ban = 50
        needed_power_level = max(
            state_default_int, ban, max(event_power_levels.values(), default=0)
        )

        # Get the user's current power level, this matches the logic in get_user_power_level,
        # but without the entire state map.
        user_power_levels = power_levels.setdefault("users", {})
        if not isinstance(user_power_levels, dict):
            user_power_levels = {}
        users_default = power_levels.get("users_default", 0)
        current_power_level = user_power_levels.get(user_id, users_default)
        try:
            current_power_level_int = int(current_power_level)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            current_power_level_int = 0
        # Raise the requester's power level in the new room if necessary
        if current_power_level_int < needed_power_level:
            user_power_levels[user_id] = needed_power_level

        await self._send_events_for_new_room(
            requester,
            new_room_id,
            new_room_version,
            # we expect to override all the presets with initial_state, so this is
            # somewhat arbitrary.
            room_config={"preset": RoomCreationPreset.PRIVATE_CHAT},
            invite_list=[],
            initial_state=initial_state,
            creation_content=creation_content,
        )

        # Transfer membership events
        old_room_member_state_ids = (
            await self._storage_controllers.state.get_current_state_ids(
                old_room_id, StateFilter.from_types([(EventTypes.Member, None)])
            )
        )

        # map from event_id to BaseEvent
        old_room_member_state_events = await self.store.get_events(
            old_room_member_state_ids.values()
        )
        for old_event in old_room_member_state_events.values():
            # Only transfer ban events
            if (
                "membership" in old_event.content
                and old_event.content["membership"] == "ban"
            ):
                await self.room_member_handler.update_membership(
                    requester,
                    UserID.from_string(old_event.state_key),
                    new_room_id,
                    "ban",
                    ratelimit=False,
                    content=old_event.content,
                )

        # XXX invites/joins
        # XXX 3pid invites

    async def _move_aliases_to_new_room(
        self,
        requester: Requester,
        old_room_id: str,
        new_room_id: str,
        old_room_state: StateMap[str],
    ) -> None:
        # check to see if we have a canonical alias.
        canonical_alias_event = None
        canonical_alias_event_id = old_room_state.get((EventTypes.CanonicalAlias, ""))
        if canonical_alias_event_id:
            canonical_alias_event = await self.store.get_event(canonical_alias_event_id)

        await self.store.update_aliases_for_room(old_room_id, new_room_id)

        if not canonical_alias_event:
            return

        # If there is a canonical alias we need to update the one in the old
        # room and set one in the new one.
        old_canonical_alias_content = dict(canonical_alias_event.content)
        new_canonical_alias_content = {}

        canonical = canonical_alias_event.content.get("alias")
        if canonical and self.hs.is_mine_id(canonical):
            new_canonical_alias_content["alias"] = canonical
            old_canonical_alias_content.pop("alias", None)

        # We convert to a list as it will be a Tuple.
        old_alt_aliases = list(old_canonical_alias_content.get("alt_aliases", []))
        if old_alt_aliases:
            old_canonical_alias_content["alt_aliases"] = old_alt_aliases
            new_alt_aliases = new_canonical_alias_content.setdefault("alt_aliases", [])
            for alias in canonical_alias_event.content.get("alt_aliases", []):
                try:
                    if self.hs.is_mine_id(alias):
                        new_alt_aliases.append(alias)
                        old_alt_aliases.remove(alias)
                except Exception:
                    logger.info(
                        "Invalid alias %s in canonical alias event %s",
                        alias,
                        canonical_alias_event_id,
                    )

            if not old_alt_aliases:
                old_canonical_alias_content.pop("alt_aliases")

        # If a canonical alias event existed for the old room, fire a canonical
        # alias event for the new room with a copy of the information.
        try:
            await self.event_creation_handler.create_and_send_nonmember_event(
                requester,
                {
                    "type": EventTypes.CanonicalAlias,
                    "state_key": "",
                    "room_id": old_room_id,
                    "sender": requester.user.to_string(),
                    "content": old_canonical_alias_content,
                },
                ratelimit=False,
            )
        except SynapseError as e:
            # again I'm not really expecting this to fail, but if it does, I'd rather
            # we returned the new room to the client at this point.
            logger.error("Unable to send updated alias events in old room: %s", e)

        try:
            await self.event_creation_handler.create_and_send_nonmember_event(
                requester,
                {
                    "type": EventTypes.CanonicalAlias,
                    "state_key": "",
                    "room_id": new_room_id,
                    "sender": requester.user.to_string(),
                    "content": new_canonical_alias_content,
                },
                ratelimit=False,
            )
        except SynapseError as e:
            # again I'm not really expecting this to fail, but if it does, I'd rather
            # we returned the new room to the client at this point.
            logger.error("Unable to send updated alias events in new room: %s", e)

    async def create_room(
        self,
        requester: Requester,
        config: JsonDict,
        ratelimit: bool = True,
        creator_join_profile: Optional[JsonDict] = None,
        ignore_forced_encryption: bool = False,
    ) -> Tuple[str, Optional[RoomAlias], int]:
        """Creates a new room.

        Args:
            requester: The user who requested the room creation.
            config: A dict of configuration options. This will be the body of
                a /createRoom request; see
                https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom
            ratelimit: set to False to disable the rate limiter

            creator_join_profile:
                Set to override the displayname and avatar for the creating
                user in this room. If unset, displayname and avatar will be
                derived from the user's profile. If set, should contain the
                values to go in the body of the 'join' event (typically
                `avatar_url` and/or `displayname`.
            ignore_forced_encryption:
                Ignore encryption forced by `encryption_enabled_by_default_for_room_type` setting.

        Returns:
            A 3-tuple containing:
                - the room ID;
                - if requested, the room alias, otherwise None; and
                - the `stream_id` of the last persisted event.
        Raises:
            SynapseError:
                if the room ID couldn't be stored, 3pid invitation config
                validation failed, or something went horribly wrong.
            ResourceLimitError:
                if server is blocked to some resource being
                exceeded
        """
        user_id = requester.user.to_string()

        await self.auth_blocking.check_auth_blocking(requester=requester)

        if (
            self._server_notices_mxid is not None
            and user_id == self._server_notices_mxid
        ):
            # allow the server notices mxid to create rooms
            is_requester_admin = True
        else:
            is_requester_admin = await self.auth.is_server_admin(requester)

        # Let the third party rules modify the room creation config if needed, or abort
        # the room creation entirely with an exception.
        await self._third_party_event_rules.on_create_room(
            requester, config, is_requester_admin=is_requester_admin
        )

        invite_3pid_list = config.get("invite_3pid", [])
        invite_list = config.get("invite", [])

        # validate each entry for correctness
        for invite_3pid in invite_3pid_list:
            if not all(
                key in invite_3pid
                for key in ("medium", "address", "id_server", "id_access_token")
            ):
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST,
                    "all of `medium`, `address`, `id_server` and `id_access_token` "
                    "are required when making a 3pid invite",
                    Codes.MISSING_PARAM,
                )

        if not is_requester_admin:
            spam_check = await self._spam_checker_module_callbacks.user_may_create_room(
                user_id
            )
            if spam_check != self._spam_checker_module_callbacks.NOT_SPAM:
                raise SynapseError(
                    403,
                    "You are not permitted to create rooms",
                    errcode=spam_check[0],
                    additional_fields=spam_check[1],
                )

        if ratelimit:
            # Rate limit once in advance, but don't rate limit the individual
            # events in the room — room creation isn't atomic and it's very
            # janky if half the events in the initial state don't make it because
            # of rate limiting.
            await self.request_ratelimiter.ratelimit(requester)

        room_version_id = config.get(
            "room_version", self.config.server.default_room_version.identifier
        )

        if not isinstance(room_version_id, str):
            raise SynapseError(400, "room_version must be a string", Codes.BAD_JSON)

        room_version = KNOWN_ROOM_VERSIONS.get(room_version_id)
        if room_version is None:
            raise SynapseError(
                400,
                "Your homeserver does not support this room version",
                Codes.UNSUPPORTED_ROOM_VERSION,
            )

        room_alias = None
        if "room_alias_name" in config:
            for wchar in string.whitespace:
                if wchar in config["room_alias_name"]:
                    raise SynapseError(400, "Invalid characters in room alias")

            if ":" in config["room_alias_name"]:
                # Prevent someone from trying to pass in a full alias here.
                # Note that it's permissible for a room alias to have multiple
                # hash symbols at the start (notably bridged over from IRC, too),
                # but the first colon in the alias is defined to separate the local
                # part from the server name.
                # (remember server names can contain port numbers, also separated
                # by a colon. But under no circumstances should the local part be
                # allowed to contain a colon!)
                raise SynapseError(
                    400,
                    "':' is not permitted in the room alias name. "
                    "Please note this expects a local part — 'wombat', not '#wombat:example.com'.",
                )

            room_alias = RoomAlias(config["room_alias_name"], self.hs.hostname)
            mapping = await self.store.get_association_from_room_alias(room_alias)

            if mapping:
                raise SynapseError(400, "Room alias already taken", Codes.ROOM_IN_USE)

        for i in invite_list:
            try:
                uid = UserID.from_string(i)
                parse_and_validate_server_name(uid.domain)
            except Exception:
                raise SynapseError(400, "Invalid user_id: %s" % (i,))

        if (invite_list or invite_3pid_list) and requester.shadow_banned:
            # We randomly sleep a bit just to annoy the requester.
            await self.clock.sleep(random.randint(1, 10))

            # Allow the request to go through, but remove any associated invites.
            invite_3pid_list = []
            invite_list = []

        if invite_list or invite_3pid_list:
            try:
                # If there are invites in the request, see if the ratelimiting settings
                # allow that number of invites to be sent from the current user.
                await self.room_member_handler.ratelimit_multiple_invites(
                    requester,
                    room_id=None,
                    n_invites=len(invite_list) + len(invite_3pid_list),
                    update=False,
                )
            except LimitExceededError:
                raise SynapseError(400, "Cannot invite so many users at once")

        await self.event_creation_handler.assert_accepted_privacy_policy(requester)

        power_level_content_override = config.get("power_level_content_override")
        if (
            power_level_content_override
            and "users" in power_level_content_override
            and user_id not in power_level_content_override["users"]
        ):
            raise SynapseError(
                400,
                "Not a valid power_level_content_override: 'users' did not contain %s"
                % (user_id,),
            )

        # The spec says rooms should default to private visibility if
        # `visibility` is not specified.
        visibility = config.get("visibility", "private")
        is_public = visibility == "public"

        self._validate_room_config(config, visibility)

        room_id = await self._generate_and_create_room_id(
            creator_id=user_id,
            is_public=is_public,
            room_version=room_version,
        )

        # Check whether this visibility value is blocked by a third party module
        allowed_by_third_party_rules = (
            await (
                self._third_party_event_rules.check_visibility_can_be_modified(
                    room_id, visibility
                )
            )
        )
        if not allowed_by_third_party_rules:
            raise SynapseError(403, "Room visibility value not allowed.")

        if is_public:
            room_aliases = []
            if room_alias:
                room_aliases.append(room_alias.to_string())
            if not self.config.roomdirectory.is_publishing_room_allowed(
                user_id, room_id, room_aliases
            ):
                # allow room creation to continue but do not publish room
                await self.store.set_room_is_public(room_id, False)

        directory_handler = self.hs.get_directory_handler()
        if room_alias:
            await directory_handler.create_association(
                requester=requester,
                room_id=room_id,
                room_alias=room_alias,
                servers=[self.hs.hostname],
                check_membership=False,
            )

        raw_initial_state = config.get("initial_state", [])

        initial_state = OrderedDict()
        for val in raw_initial_state:
            initial_state[(val["type"], val.get("state_key", ""))] = val["content"]

        creation_content = config.get("creation_content", {})

        # override any attempt to set room versions via the creation_content
        creation_content["room_version"] = room_version.identifier

        (
            last_stream_id,
            last_sent_event_id,
            depth,
        ) = await self._send_events_for_new_room(
            requester,
            room_id,
            room_version,
            room_config=config,
            invite_list=invite_list,
            initial_state=initial_state,
            creation_content=creation_content,
            room_alias=room_alias,
            power_level_content_override=power_level_content_override,
            creator_join_profile=creator_join_profile,
            ignore_forced_encryption=ignore_forced_encryption,
        )

        # we avoid dropping the lock between invites, as otherwise joins can
        # start coming in and making the createRoom slow.
        #
        # we also don't need to check the requester's shadow-ban here, as we
        # have already done so above (and potentially emptied invite_list).
        async with self.room_member_handler.member_linearizer.queue((room_id,)):
            content = {}
            is_direct = config.get("is_direct", None)
            if is_direct:
                content["is_direct"] = is_direct

            for invitee in invite_list:
                (
                    member_event_id,
                    last_stream_id,
                ) = await self.room_member_handler.update_membership_locked(
                    requester,
                    UserID.from_string(invitee),
                    room_id,
                    "invite",
                    ratelimit=False,
                    content=content,
                    new_room=True,
                    prev_event_ids=[last_sent_event_id],
                    depth=depth,
                )
                last_sent_event_id = member_event_id
                depth += 1

        for invite_3pid in invite_3pid_list:
            id_server = invite_3pid["id_server"]
            id_access_token = invite_3pid["id_access_token"]
            address = invite_3pid["address"]
            medium = invite_3pid["medium"]
            # Note that do_3pid_invite can raise a  ShadowBanError, but this was
            # handled above by emptying invite_3pid_list.
            (
                member_event_id,
                last_stream_id,
            ) = await self.hs.get_room_member_handler().do_3pid_invite(
                room_id,
                requester.user,
                medium,
                address,
                id_server,
                requester,
                txn_id=None,
                id_access_token=id_access_token,
                prev_event_ids=[last_sent_event_id],
                depth=depth,
            )
            last_sent_event_id = member_event_id
            depth += 1

        # Always wait for room creation to propagate before returning
        await self._replication.wait_for_stream_position(
            self.hs.config.worker.events_shard_config.get_instance(room_id),
            "events",
            last_stream_id,
        )

        return room_id, room_alias, last_stream_id

    async def _send_events_for_new_room(
        self,
        creator: Requester,
        room_id: str,
        room_version: RoomVersion,
        room_config: JsonDict,
        invite_list: List[str],
        initial_state: MutableStateMap,
        creation_content: JsonDict,
        room_alias: Optional[RoomAlias] = None,
        power_level_content_override: Optional[JsonDict] = None,
        creator_join_profile: Optional[JsonDict] = None,
        ignore_forced_encryption: bool = False,
    ) -> Tuple[int, str, int]:
        """Sends the initial events into a new room. Sends the room creation, membership,
        and power level events into the room sequentially, then creates and batches up the
        rest of the events to persist as a batch to the DB.

        `power_level_content_override` doesn't apply when initial state has
        power level state event content.

        Rate limiting should already have been applied by this point.

        Args:
            creator:
                the user requesting the room creation
            room_id:
                room id for the room being created
            room_version:
                The room version of the new room.
            room_config:
                A dict of configuration options. This will be the body of
                a /createRoom request; see
                https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3createroom
            invite_list:
                a list of user ids to invite to the room
            initial_state:
                A list of state events to set in the new room.
            creation_content:
                Extra keys, such as m.federate, to be added to the content of the m.room.create event.
            room_alias:
                alias for the room
            power_level_content_override:
                The power level content to override in the default power level event.
            creator_join_profile:
                Set to override the displayname and avatar for the creating
                user in this room.
            ignore_forced_encryption:
                Ignore encryption forced by `encryption_enabled_by_default_for_room_type` setting.

        Returns:
            A tuple containing the stream ID, event ID and depth of the last
            event sent to the room.
        """
        creator_id = creator.user.to_string()
        event_keys = {"room_id": room_id, "sender": creator_id, "state_key": ""}
        depth = 1

        # the most recently created event
        prev_event: List[str] = []
        # a map of event types, state keys -> event_ids. We collect these mappings this as events are
        # created (but not persisted to the db) to determine state for future created events
        # (as this info can't be pulled from the db)
        state_map: MutableStateMap[str] = {}

        async def create_event(
            etype: str,
            content: JsonDict,
            for_batch: bool,
            **kwargs: Any,
        ) -> Tuple[EventBase, synapse.events.snapshot.UnpersistedEventContextBase]:
            """
            Creates an event and associated event context.
            Args:
                etype: the type of event to be created
                content: content of the event
                for_batch: whether the event is being created for batch persisting. If
                bool for_batch is true, this will create an event using the prev_event_ids,
                and will create an event context for the event using the parameters state_map
                and current_state_group, thus these parameters must be provided in this
                case if for_batch is True. The subsequently created event and context
                are suitable for being batched up and bulk persisted to the database
                with other similarly created events.
            """
            nonlocal depth
            nonlocal prev_event

            # Create the event dictionary.
            event_dict = {"type": etype, "content": content}
            event_dict.update(event_keys)
            event_dict.update(kwargs)

            (
                new_event,
                new_unpersisted_context,
            ) = await self.event_creation_handler.create_event(
                creator,
                event_dict,
                prev_event_ids=prev_event,
                depth=depth,
                # Take a copy to ensure each event gets a unique copy of
                # state_map since it is modified below.
                state_map=dict(state_map),
                for_batch=for_batch,
            )

            depth += 1
            prev_event = [new_event.event_id]
            state_map[(new_event.type, new_event.state_key)] = new_event.event_id

            return new_event, new_unpersisted_context

        preset_config, config = self._room_preset_config(room_config)

        # MSC2175 removes the creator field from the create event.
        if not room_version.implicit_room_creator:
            creation_content["creator"] = creator_id
        creation_event, unpersisted_creation_context = await create_event(
            EventTypes.Create, creation_content, False
        )
        creation_context = await unpersisted_creation_context.persist(creation_event)
        logger.debug("Sending %s in new room", EventTypes.Member)
        ev = await self.event_creation_handler.handle_new_client_event(
            requester=creator,
            events_and_context=[(creation_event, creation_context)],
            ratelimit=False,
            ignore_shadow_ban=True,
        )
        last_sent_event_id = ev.event_id

        member_event_id, _ = await self.room_member_handler.update_membership(
            creator,
            creator.user,
            room_id,
            "join",
            ratelimit=False,
            content=creator_join_profile,
            new_room=True,
            prev_event_ids=[last_sent_event_id],
            depth=depth,
        )
        prev_event = [member_event_id]

        # update the depth and state map here as the membership event has been created
        # through a different code path
        depth += 1
        state_map[(EventTypes.Member, creator.user.to_string())] = member_event_id

        # we need the state group of the membership event as it is the current state group
        event_to_state = (
            await self._storage_controllers.state.get_state_group_for_events(
                [member_event_id]
            )
        )
        current_state_group = event_to_state[member_event_id]

        events_to_send = []
        # We treat the power levels override specially as this needs to be one
        # of the first events that get sent into a room.
        pl_content = initial_state.pop((EventTypes.PowerLevels, ""), None)
        if pl_content is not None:
            power_event, power_context = await create_event(
                EventTypes.PowerLevels, pl_content, True
            )
            events_to_send.append((power_event, power_context))
        else:
            # Please update the docs for `default_power_level_content_override` when
            # updating the `events` dict below
            power_level_content: JsonDict = {
                "users": {creator_id: 100},
                "users_default": 0,
                "events": {
                    EventTypes.Name: 50,
                    EventTypes.PowerLevels: 100,
                    EventTypes.RoomHistoryVisibility: 100,
                    EventTypes.CanonicalAlias: 50,
                    EventTypes.RoomAvatar: 50,
                    EventTypes.Tombstone: 100,
                    EventTypes.ServerACL: 100,
                    EventTypes.RoomEncryption: 100,
                },
                "events_default": 0,
                "state_default": 50,
                "ban": 50,
                "kick": 50,
                "redact": 50,
                "invite": 50,
                "historical": 100,
            }

            if config["original_invitees_have_ops"]:
                for invitee in invite_list:
                    power_level_content["users"][invitee] = 100

            # If the user supplied a preset name e.g. "private_chat",
            # we apply that preset
            power_level_content.update(config["power_level_content_override"])

            # If the server config contains default_power_level_content_override,
            # and that contains information for this room preset, apply it.
            if self._default_power_level_content_override:
                override = self._default_power_level_content_override.get(preset_config)
                if override is not None:
                    power_level_content.update(override)

            # Finally, if the user supplied specific permissions for this room,
            # apply those.
            if power_level_content_override:
                power_level_content.update(power_level_content_override)
            pl_event, pl_context = await create_event(
                EventTypes.PowerLevels,
                power_level_content,
                True,
            )
            events_to_send.append((pl_event, pl_context))

        if room_alias and (EventTypes.CanonicalAlias, "") not in initial_state:
            room_alias_event, room_alias_context = await create_event(
                EventTypes.CanonicalAlias, {"alias": room_alias.to_string()}, True
            )
            events_to_send.append((room_alias_event, room_alias_context))

        if (EventTypes.JoinRules, "") not in initial_state:
            join_rules_event, join_rules_context = await create_event(
                EventTypes.JoinRules,
                {"join_rule": config["join_rules"]},
                True,
            )
            events_to_send.append((join_rules_event, join_rules_context))

        if (EventTypes.RoomHistoryVisibility, "") not in initial_state:
            visibility_event, visibility_context = await create_event(
                EventTypes.RoomHistoryVisibility,
                {"history_visibility": config["history_visibility"]},
                True,
            )
            events_to_send.append((visibility_event, visibility_context))

        if config["guest_can_join"]:
            if (EventTypes.GuestAccess, "") not in initial_state:
                guest_access_event, guest_access_context = await create_event(
                    EventTypes.GuestAccess,
                    {EventContentFields.GUEST_ACCESS: GuestAccess.CAN_JOIN},
                    True,
                )
                events_to_send.append((guest_access_event, guest_access_context))

        for (etype, state_key), content in initial_state.items():
            event, context = await create_event(
                etype, content, True, state_key=state_key
            )
            events_to_send.append((event, context))

        if config["encrypted"] and not ignore_forced_encryption:
            encryption_event, encryption_context = await create_event(
                EventTypes.RoomEncryption,
                {"algorithm": RoomEncryptionAlgorithms.DEFAULT},
                True,
                state_key="",
            )
            events_to_send.append((encryption_event, encryption_context))

        if "name" in room_config:
            name = room_config["name"]
            name_event, name_context = await create_event(
                EventTypes.Name,
                {"name": name},
                True,
            )
            events_to_send.append((name_event, name_context))

        if "topic" in room_config:
            topic = room_config["topic"]
            topic_event, topic_context = await create_event(
                EventTypes.Topic,
                {"topic": topic},
                True,
            )
            events_to_send.append((topic_event, topic_context))

        datastore = self.hs.get_datastores().state
        events_and_context = (
            await UnpersistedEventContext.batch_persist_unpersisted_contexts(
                events_to_send, room_id, current_state_group, datastore
            )
        )

        last_event = await self.event_creation_handler.handle_new_client_event(
            creator,
            events_and_context,
            ignore_shadow_ban=True,
            ratelimit=False,
        )
        assert last_event.internal_metadata.stream_ordering is not None
        return last_event.internal_metadata.stream_ordering, last_event.event_id, depth

    def _validate_room_config(
        self,
        config: JsonDict,
        visibility: str,
    ) -> None:
        """Checks configuration parameters for a /createRoom request.

        If validation detects invalid parameters an exception may be raised to
        cause room creation to be aborted and an error response to be returned
        to the client.

        Args:
            config: A dict of configuration options. Originally from the body of
                the /createRoom request
            visibility: One of "public" or "private"
        """

        # Validate the requested preset, raise a 400 error if not valid
        preset_name, preset_config = self._room_preset_config(config)

        # If the user is trying to create an encrypted room and this is forbidden
        # by the configured default_power_level_content_override, then reject the
        # request before the room is created.
        raw_initial_state = config.get("initial_state", [])
        room_encryption_event = any(
            s.get("type", "") == EventTypes.RoomEncryption for s in raw_initial_state
        )

        if preset_config["encrypted"] or room_encryption_event:
            if self._default_power_level_content_override:
                override = self._default_power_level_content_override.get(preset_name)
                if override is not None:
                    event_levels = override.get("events", {})
                    room_admin_level = event_levels.get(EventTypes.PowerLevels, 100)
                    encryption_level = event_levels.get(EventTypes.RoomEncryption, 100)
                    if encryption_level > room_admin_level:
                        raise SynapseError(
                            403,
                            f"You cannot create an encrypted room. user_level ({room_admin_level}) < send_level ({encryption_level})",
                        )

    def _room_preset_config(self, room_config: JsonDict) -> Tuple[str, dict]:
        # The spec says rooms should default to private visibility if
        # `visibility` is not specified.
        visibility = room_config.get("visibility", "private")
        preset_name = room_config.get(
            "preset",
            (
                RoomCreationPreset.PRIVATE_CHAT
                if visibility == "private"
                else RoomCreationPreset.PUBLIC_CHAT
            ),
        )
        try:
            preset_config = self._presets_dict[preset_name]
        except KeyError:
            raise SynapseError(
                400, f"'{preset_name}' is not a valid preset", errcode=Codes.BAD_JSON
            )
        return preset_name, preset_config

    def _generate_room_id(self) -> str:
        """Generates a random room ID.

        Room IDs look like "!opaque_id:domain" and are case-sensitive as per the spec
        at https://spec.matrix.org/v1.2/appendices/#room-ids-and-event-ids.

        Does not check for collisions with existing rooms or prevent future calls from
        returning the same room ID. To ensure the uniqueness of a new room ID, use
        `_generate_and_create_room_id` instead.

        Synapse's room IDs are 18 [a-zA-Z] characters long, which comes out to around
        102 bits.

        Returns:
            A random room ID of the form "!opaque_id:domain".
        """
        random_string = stringutils.random_string(18)
        return RoomID(random_string, self.hs.hostname).to_string()

    async def _generate_and_create_room_id(
        self,
        creator_id: str,
        is_public: bool,
        room_version: RoomVersion,
    ) -> str:
        # autogen room IDs and try to create it. We may clash, so just
        # try a few times till one goes through, giving up eventually.
        attempts = 0
        while attempts < 5:
            try:
                gen_room_id = self._generate_room_id()
                await self.store.store_room(
                    room_id=gen_room_id,
                    room_creator_user_id=creator_id,
                    is_public=is_public,
                    room_version=room_version,
                )
                return gen_room_id
            except StoreError:
                attempts += 1
        raise StoreError(500, "Couldn't generate a room ID.")


class RoomContextHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.auth = hs.get_auth()
        self.store = hs.get_datastores().main
        self._storage_controllers = hs.get_storage_controllers()
        self._state_storage_controller = self._storage_controllers.state
        self._relations_handler = hs.get_relations_handler()

    async def get_event_context(
        self,
        requester: Requester,
        room_id: str,
        event_id: str,
        limit: int,
        event_filter: Optional[Filter],
        use_admin_priviledge: bool = False,
    ) -> Optional[EventContext]:
        """Retrieves events, pagination tokens and state around a given event
        in a room.

        Args:
            requester
            room_id
            event_id
            limit: The maximum number of events to return in total
                (excluding state).
            event_filter: the filter to apply to the events returned
                (excluding the target event_id)
            use_admin_priviledge: if `True`, return all events, regardless
                of whether `user` has access to them. To be used **ONLY**
                from the admin API.
        Returns:
            dict, or None if the event isn't found
        """
        user = requester.user
        if use_admin_priviledge:
            await assert_user_is_admin(self.auth, requester)

        before_limit = math.floor(limit / 2.0)
        after_limit = limit - before_limit

        is_user_in_room = await self.store.check_local_user_in_room(
            user_id=user.to_string(), room_id=room_id
        )
        # The user is peeking if they aren't in the room already
        is_peeking = not is_user_in_room

        async def filter_evts(events: List[EventBase]) -> List[EventBase]:
            if use_admin_priviledge:
                return events
            return await filter_events_for_client(
                self._storage_controllers,
                user.to_string(),
                events,
                is_peeking=is_peeking,
            )

        event = await self.store.get_event(
            event_id, get_prev_content=True, allow_none=True
        )
        if not event:
            return None

        filtered = await filter_evts([event])
        if not filtered:
            raise AuthError(403, "You don't have permission to access that event.")

        results = await self.store.get_events_around(
            room_id, event_id, before_limit, after_limit, event_filter
        )
        events_before = results.events_before
        events_after = results.events_after

        if event_filter:
            events_before = await event_filter.filter(events_before)
            events_after = await event_filter.filter(events_after)

        events_before = await filter_evts(events_before)
        events_after = await filter_evts(events_after)
        # filter_evts can return a pruned event in case the user is allowed to see that
        # there's something there but not see the content, so use the event that's in
        # `filtered` rather than the event we retrieved from the datastore.
        event = filtered[0]

        # Fetch the aggregations.
        aggregations = await self._relations_handler.get_bundled_aggregations(
            itertools.chain(events_before, (event,), events_after),
            user.to_string(),
        )

        if events_after:
            last_event_id = events_after[-1].event_id
        else:
            last_event_id = event_id

        if event_filter and event_filter.lazy_load_members:
            state_filter = StateFilter.from_lazy_load_member_list(
                ev.sender
                for ev in itertools.chain(
                    events_before,
                    (event,),
                    events_after,
                )
            )
        else:
            state_filter = StateFilter.all()

        # XXX: why do we return the state as of the last event rather than the
        # first? Shouldn't we be consistent with /sync?
        # https://github.com/matrix-org/matrix-doc/issues/687

        state = await self._state_storage_controller.get_state_for_events(
            [last_event_id], state_filter=state_filter
        )

        state_events = list(state[last_event_id].values())
        if event_filter:
            state_events = await event_filter.filter(state_events)

        # We use a dummy token here as we only care about the room portion of
        # the token, which we replace.
        token = StreamToken.START

        return EventContext(
            events_before=events_before,
            event=event,
            events_after=events_after,
            state=state_events,
            aggregations=aggregations,
            start=await token.copy_and_replace(
                StreamKeyType.ROOM, results.start
            ).to_string(self.store),
            end=await token.copy_and_replace(StreamKeyType.ROOM, results.end).to_string(
                self.store
            ),
        )


class TimestampLookupHandler:
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.state_handler = hs.get_state_handler()
        self.federation_client = hs.get_federation_client()
        self.federation_event_handler = hs.get_federation_event_handler()
        self._storage_controllers = hs.get_storage_controllers()

    async def get_event_for_timestamp(
        self,
        requester: Requester,
        room_id: str,
        timestamp: int,
        direction: Direction,
    ) -> Tuple[str, int]:
        """Find the closest event to the given timestamp in the given direction.
        If we can't find an event locally or the event we have locally is next to a gap,
        it will ask other federated homeservers for an event.

        Args:
            requester: The user making the request according to the access token
            room_id: Room to fetch the event from
            timestamp: The point in time (inclusive) we should navigate from in
                the given direction to find the closest event.
            direction: indicates whether we should navigate forward
                or backward from the given timestamp to find the closest event.

        Returns:
            A tuple containing the `event_id` closest to the given timestamp in
            the given direction and the `origin_server_ts`.

        Raises:
            SynapseError if unable to find any event locally in the given direction
        """
        logger.debug(
            "get_event_for_timestamp(room_id=%s, timestamp=%s, direction=%s) Finding closest event...",
            room_id,
            timestamp,
            direction,
        )
        local_event_id = await self.store.get_event_id_for_timestamp(
            room_id, timestamp, direction
        )
        logger.debug(
            "get_event_for_timestamp: locally, we found event_id=%s closest to timestamp=%s",
            local_event_id,
            timestamp,
        )

        # Check for gaps in the history where events could be hiding in between
        # the timestamp given and the event we were able to find locally
        is_event_next_to_backward_gap = False
        is_event_next_to_forward_gap = False
        local_event = None
        if local_event_id:
            local_event = await self.store.get_event(
                local_event_id, allow_none=False, allow_rejected=False
            )

            if direction == Direction.FORWARDS:
                # We only need to check for a backward gap if we're looking forwards
                # to ensure there is nothing in between.
                is_event_next_to_backward_gap = (
                    await self.store.is_event_next_to_backward_gap(local_event)
                )
            elif direction == Direction.BACKWARDS:
                # We only need to check for a forward gap if we're looking backwards
                # to ensure there is nothing in between
                is_event_next_to_forward_gap = (
                    await self.store.is_event_next_to_forward_gap(local_event)
                )

        # If we found a gap, we should probably ask another homeserver first
        # about more history in between
        if (
            not local_event_id
            or is_event_next_to_backward_gap
            or is_event_next_to_forward_gap
        ):
            logger.debug(
                "get_event_for_timestamp: locally, we found event_id=%s closest to timestamp=%s which is next to a gap in event history so we're asking other homeservers first",
                local_event_id,
                timestamp,
            )

            likely_domains = (
                await self._storage_controllers.state.get_current_hosts_in_room_ordered(
                    room_id
                )
            )

            remote_response = await self.federation_client.timestamp_to_event(
                destinations=likely_domains,
                room_id=room_id,
                timestamp=timestamp,
                direction=direction,
            )
            if remote_response is not None:
                logger.debug(
                    "get_event_for_timestamp: remote_response=%s",
                    remote_response,
                )

                remote_event_id = remote_response.event_id
                remote_origin_server_ts = remote_response.origin_server_ts

                # Backfill this event so we can get a pagination token for
                # it with `/context` and paginate `/messages` from this
                # point.
                pulled_pdu_info = await self.federation_event_handler.backfill_event_id(
                    likely_domains, room_id, remote_event_id
                )
                remote_event = pulled_pdu_info.pdu

                # XXX: When we see that the remote server is not trustworthy,
                # maybe we should not ask them first in the future.
                if remote_origin_server_ts != remote_event.origin_server_ts:
                    logger.info(
                        "get_event_for_timestamp: Remote server (%s) claimed that remote_event_id=%s occured at remote_origin_server_ts=%s but that isn't true (actually occured at %s). Their claims are dubious and we should consider not trusting them.",
                        pulled_pdu_info.pull_origin,
                        remote_event_id,
                        remote_origin_server_ts,
                        remote_event.origin_server_ts,
                    )

                # Only return the remote event if it's closer than the local event
                if not local_event or (
                    abs(remote_event.origin_server_ts - timestamp)
                    < abs(local_event.origin_server_ts - timestamp)
                ):
                    logger.info(
                        "get_event_for_timestamp: returning remote_event_id=%s (%s) since it's closer to timestamp=%s than local_event=%s (%s)",
                        remote_event_id,
                        remote_event.origin_server_ts,
                        timestamp,
                        local_event.event_id if local_event else None,
                        local_event.origin_server_ts if local_event else None,
                    )
                    return remote_event_id, remote_origin_server_ts

        # To appease mypy, we have to add both of these conditions to check for
        # `None`. We only expect `local_event` to be `None` when
        # `local_event_id` is `None` but mypy isn't as smart and assuming as us.
        if not local_event_id or not local_event:
            raise SynapseError(
                404,
                "Unable to find event from %s in direction %s" % (timestamp, direction),
                errcode=Codes.NOT_FOUND,
            )

        return local_event_id, local_event.origin_server_ts


class RoomEventSource(EventSource[RoomStreamToken, EventBase]):
    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main

    async def get_new_events(
        self,
        user: UserID,
        from_key: RoomStreamToken,
        limit: int,
        room_ids: StrCollection,
        is_guest: bool,
        explicit_room_id: Optional[str] = None,
    ) -> Tuple[List[EventBase], RoomStreamToken]:
        # We just ignore the key for now.

        to_key = self.get_current_key()

        if from_key.topological:
            logger.warning("Stream has topological part!!!! %r", from_key)
            from_key = RoomStreamToken(stream=from_key.stream)

        app_service = self.store.get_app_service_by_user_id(user.to_string())
        if app_service:
            # We no longer support AS users using /sync directly.
            # See https://github.com/matrix-org/matrix-doc/issues/1144
            raise NotImplementedError()
        else:
            room_events = await self.store.get_membership_changes_for_user(
                user.to_string(), from_key, to_key
            )

            room_to_events = await self.store.get_room_events_stream_for_rooms(
                room_ids=room_ids,
                from_key=from_key,
                to_key=to_key,
                limit=limit or 10,
                direction=Direction.FORWARDS,
            )

            events = list(room_events)
            events.extend(e for evs, _ in room_to_events.values() for e in evs)

            # We know stream_ordering must be not None here, as its been
            # persisted, but mypy doesn't know that
            events.sort(key=lambda e: cast(int, e.internal_metadata.stream_ordering))

            if limit:
                events[:] = events[:limit]

            if events:
                last_event = events[-1]
                assert last_event.internal_metadata.stream_ordering
                end_key = RoomStreamToken(
                    stream=last_event.internal_metadata.stream_ordering,
                )
            else:
                end_key = to_key

        return events, end_key

    def get_current_key(self) -> RoomStreamToken:
        return self.store.get_room_max_token()

    def get_current_key_for_room(self, room_id: str) -> Awaitable[RoomStreamToken]:
        return self.store.get_current_room_stream_token_for_room_id(room_id)


class RoomShutdownHandler:
    DEFAULT_MESSAGE = (
        "Sharing illegal content on this server is not permitted and rooms in"
        " violation will be blocked."
    )
    DEFAULT_ROOM_NAME = "Content Violation Notification"

    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.room_member_handler = hs.get_room_member_handler()
        self._room_creation_handler = hs.get_room_creation_handler()
        self._replication = hs.get_replication_data_handler()
        self._third_party_rules = hs.get_module_api_callbacks().third_party_event_rules
        self.event_creation_handler = hs.get_event_creation_handler()
        self.store = hs.get_datastores().main

    async def shutdown_room(
        self,
        room_id: str,
        params: ShutdownRoomParams,
        result: Optional[ShutdownRoomResponse] = None,
        update_result_fct: Optional[
            Callable[[Optional[JsonMapping]], Awaitable[None]]
        ] = None,
    ) -> Optional[ShutdownRoomResponse]:
        """
        Shuts down a room. Moves all local users and room aliases automatically
        to a new room if `new_room_user_id` is set. Otherwise local users only
        leave the room without any information.

        The new room will be created with the user specified by the
        `new_room_user_id` parameter as room administrator and will contain a
        message explaining what happened. Users invited to the new room will
        have power level `-10` by default, and thus be unable to speak.

        The local server will only have the power to move local user and room
        aliases to the new room. Users on other servers will be unaffected.

        Args:
            room_id: The ID of the room to shut down.
            delete_id: The delete ID identifying this delete request
            params: parameters for the shutdown, cf `ShutdownRoomParams`
            result: current status of the shutdown, if it was interrupted
            update_result_fct: function called when `result` is updated locally

        Returns: a dict matching `ShutdownRoomResponse`.
        """
        requester_user_id = params["requester_user_id"]
        new_room_user_id = params["new_room_user_id"]
        block = params["block"]

        new_room_name = (
            params["new_room_name"]
            if params["new_room_name"]
            else self.DEFAULT_ROOM_NAME
        )
        message = params["message"] if params["message"] else self.DEFAULT_MESSAGE

        if not RoomID.is_valid(room_id):
            raise SynapseError(400, "%s is not a legal room ID" % (room_id,))

        if not await self._third_party_rules.check_can_shutdown_room(
            requester_user_id, room_id
        ):
            raise SynapseError(
                403, "Shutdown of this room is forbidden", Codes.FORBIDDEN
            )

        result = (
            result
            if result
            else {
                "kicked_users": [],
                "failed_to_kick_users": [],
                "local_aliases": [],
                "new_room_id": None,
            }
        )

        # Action the block first (even if the room doesn't exist yet)
        if block:
            if requester_user_id is None:
                raise ValueError(
                    "shutdown_room: block=True not allowed when requester_user_id is None."
                )
            # This will work even if the room is already blocked, but that is
            # desirable in case the first attempt at blocking the room failed below.
            await self.store.block_room(room_id, requester_user_id)

        if not await self.store.get_room(room_id):
            # if we don't know about the room, there is nothing left to do.
            return result

        new_room_id = result.get("new_room_id")
        if new_room_user_id is not None and new_room_id is None:
            if not self.hs.is_mine_id(new_room_user_id):
                raise SynapseError(
                    400, "User must be our own: %s" % (new_room_user_id,)
                )

            room_creator_requester = create_requester(
                new_room_user_id, authenticated_entity=requester_user_id
            )

            new_room_id, _, stream_id = await self._room_creation_handler.create_room(
                room_creator_requester,
                config={
                    "preset": RoomCreationPreset.PUBLIC_CHAT,
                    "name": new_room_name,
                    "power_level_content_override": {"users_default": -10},
                },
                ratelimit=False,
            )

            result["new_room_id"] = new_room_id
            if update_result_fct:
                await update_result_fct(result)

            logger.info(
                "Shutting down room %r, joining to new room: %r", room_id, new_room_id
            )

            # We now wait for the create room to come back in via replication so
            # that we can assume that all the joins/invites have propagated before
            # we try and auto join below.
            await self._replication.wait_for_stream_position(
                self.hs.config.worker.events_shard_config.get_instance(new_room_id),
                "events",
                stream_id,
            )
        else:
            logger.info("Shutting down room %r", room_id)

        users = await self.store.get_local_users_related_to_room(room_id)
        for user_id, membership in users:
            # If the user is not in the room (or is banned), nothing to do.
            if membership not in (Membership.JOIN, Membership.INVITE, Membership.KNOCK):
                continue

            logger.info("Kicking %r from %r...", user_id, room_id)

            try:
                # Kick users from room
                target_requester = create_requester(
                    user_id, authenticated_entity=requester_user_id
                )
                _, stream_id = await self.room_member_handler.update_membership(
                    requester=target_requester,
                    target=target_requester.user,
                    room_id=room_id,
                    action=Membership.LEAVE,
                    content={},
                    ratelimit=False,
                    require_consent=False,
                )

                # Wait for leave to come in over replication before trying to forget.
                await self._replication.wait_for_stream_position(
                    self.hs.config.worker.events_shard_config.get_instance(room_id),
                    "events",
                    stream_id,
                )

                await self.room_member_handler.forget(
                    target_requester.user, room_id, do_not_schedule_purge=True
                )

                # Join users to new room
                if new_room_user_id:
                    assert new_room_id is not None
                    await self.room_member_handler.update_membership(
                        requester=target_requester,
                        target=target_requester.user,
                        room_id=new_room_id,
                        action=Membership.JOIN,
                        content={},
                        ratelimit=False,
                        require_consent=False,
                    )

                result["kicked_users"].append(user_id)
                if update_result_fct:
                    await update_result_fct(result)
            except Exception:
                logger.exception(
                    "Failed to leave old room and join new room for %r", user_id
                )
                result["failed_to_kick_users"].append(user_id)
                if update_result_fct:
                    await update_result_fct(result)

        # Send message in new room and move aliases
        if new_room_user_id:
            room_creator_requester = create_requester(
                new_room_user_id, authenticated_entity=requester_user_id
            )

            await self.event_creation_handler.create_and_send_nonmember_event(
                room_creator_requester,
                {
                    "type": "m.room.message",
                    "content": {"body": message, "msgtype": "m.text"},
                    "room_id": new_room_id,
                    "sender": new_room_user_id,
                },
                ratelimit=False,
            )

            result["local_aliases"] = list(
                await self.store.get_aliases_for_room(room_id)
            )

            assert new_room_id is not None
            await self.store.update_aliases_for_room(
                room_id, new_room_id, requester_user_id
            )
        else:
            result["local_aliases"] = []

        return result
