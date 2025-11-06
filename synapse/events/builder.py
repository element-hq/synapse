#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
from typing import TYPE_CHECKING, Any

import attr
from signedjson.types import SigningKey

from synapse.api.constants import MAX_DEPTH, EventTypes
from synapse.api.room_versions import (
    KNOWN_EVENT_FORMAT_VERSIONS,
    EventFormatVersions,
    RoomVersion,
)
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.event_auth import auth_types_for_event
from synapse.events import EventBase, make_event_from_dict
from synapse.state import StateHandler
from synapse.storage.databases.main import DataStore
from synapse.synapse_rust.events import EventInternalMetadata
from synapse.types import EventID, JsonDict, StrCollection
from synapse.types.state import StateFilter
from synapse.util.clock import Clock
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.handlers.event_auth import EventAuthHandler
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(slots=True, cmp=False, frozen=True, auto_attribs=True)
class EventBuilder:
    """A format independent event builder used to build up the event content
    before signing the event.

    (Note that while objects of this class are frozen, the
    content/unsigned/internal_metadata fields are still mutable)

    Attributes:
        room_version: Version of the target room
        room_id
        type
        sender
        content
        unsigned
        internal_metadata

        _state
        _auth
        _store
        _clock
        _hostname: The hostname of the server creating the event
        _signing_key: The signing key to use to sign the event as the server
    """

    _state: StateHandler
    _event_auth_handler: "EventAuthHandler"
    _store: DataStore
    _clock: Clock
    _hostname: str
    _signing_key: SigningKey

    room_version: RoomVersion

    # MSC4291 makes the room ID == the create event ID. This means the create event has no room_id.
    room_id: str | None
    type: str
    sender: str

    content: JsonDict = attr.Factory(dict)
    unsigned: JsonDict = attr.Factory(dict)

    # These only exist on a subset of events, so they raise AttributeError if
    # someone tries to get them when they don't exist.
    _state_key: str | None = None
    _redacts: str | None = None
    _origin_server_ts: int | None = None

    internal_metadata: EventInternalMetadata = attr.Factory(
        lambda: EventInternalMetadata({})
    )

    @property
    def state_key(self) -> str:
        if self._state_key is not None:
            return self._state_key

        raise AttributeError("state_key")

    def is_state(self) -> bool:
        return self._state_key is not None

    def is_mine_id(self, user_id: str) -> bool:
        """Determines whether a user ID or room alias originates from this homeserver.

        Returns:
            `True` if the hostname part of the user ID or room alias matches this
            homeserver.
            `False` otherwise, or if the user ID or room alias is malformed.
        """
        localpart_hostname = user_id.split(":", 1)
        if len(localpart_hostname) < 2:
            return False
        return localpart_hostname[1] == self._hostname

    async def build(
        self,
        prev_event_ids: list[str],
        auth_event_ids: list[str] | None,
        depth: int | None = None,
    ) -> EventBase:
        """Transform into a fully signed and hashed event

        Args:
            prev_event_ids: The event IDs to use as the prev events
            auth_event_ids: The event IDs to use as the auth events.
                Should normally be set to None, which will cause them to be calculated
                based on the room state at the prev_events.
            depth: Override the depth used to order the event in the DAG.
                Should normally be set to None, which will cause the depth to be calculated
                based on the prev_events.

        Returns:
            The signed and hashed event.
        """
        # Create events always have empty auth_events.
        if self.type == EventTypes.Create and self.is_state() and self.state_key == "":
            auth_event_ids = []

        # Calculate auth_events for non-create events
        if auth_event_ids is None:
            # Every non-create event must have a room ID
            assert self.room_id is not None
            state_ids = await self._state.compute_state_after_events(
                self.room_id,
                prev_event_ids,
                state_filter=StateFilter.from_types(
                    auth_types_for_event(self.room_version, self)
                ),
                await_full_state=False,
            )
            auth_event_ids = self._event_auth_handler.compute_auth_events(
                self, state_ids
            )

            # Check for out-of-band membership that may have been exposed on `/sync` but
            # the events have not been de-outliered yet so they won't be part of the
            # room state yet.
            #
            # This helps in situations where a remote homeserver invites a local user to
            # a room that we're already participating in; and we've persisted the invite
            # as an out-of-band membership (outlier), but it hasn't been pushed to us as
            # part of a `/send` transaction yet and de-outliered. This also helps for
            # any of the other out-of-band membership transitions.
            #
            # As an optimization, we could check if the room state already includes a
            # non-`leave` membership event, then we can assume the membership event has
            # been de-outliered and we don't need to check for an out-of-band
            # membership. But we don't have the necessary information from a
            # `StateMap[str]` and we'll just have to take the hit of this extra lookup
            # for any membership event for now.
            if self.type == EventTypes.Member and self.is_mine_id(self.state_key):
                (
                    _membership,
                    member_event_id,
                ) = await self._store.get_local_current_membership_for_user_in_room(
                    user_id=self.state_key,
                    room_id=self.room_id,
                )
                # There is no need to check if the membership is actually an
                # out-of-band membership (`outlier`) as we would end up with the
                # same result either way (adding the member event to the
                # `auth_event_ids`).
                if (
                    member_event_id is not None
                    # We only need to be careful about duplicating the event in the
                    # `auth_event_ids` list (duplicate `type`/`state_key` is part of the
                    # authorization rules)
                    and member_event_id not in auth_event_ids
                ):
                    auth_event_ids.append(member_event_id)
                    # Also make sure to point to the previous membership event that will
                    # allow this one to happen so the computed state works out.
                    prev_event_ids.append(member_event_id)

        format_version = self.room_version.event_format
        # The types of auth/prev events changes between event versions.
        prev_events: StrCollection | list[tuple[str, dict[str, str]]]
        auth_events: list[str] | list[tuple[str, dict[str, str]]]
        if format_version == EventFormatVersions.ROOM_V1_V2:
            auth_events = await self._store.add_event_hashes(auth_event_ids)
            prev_events = await self._store.add_event_hashes(prev_event_ids)
        else:
            auth_events = auth_event_ids
            prev_events = prev_event_ids

        # Otherwise, progress the depth as normal
        if depth is None:
            (
                _,
                most_recent_prev_event_depth,
            ) = await self._store.get_max_depth_of(prev_event_ids)

            depth = most_recent_prev_event_depth + 1

        # we cap depth of generated events, to ensure that they are not
        # rejected by other servers (and so that they can be persisted in
        # the db)
        depth = min(depth, MAX_DEPTH)

        event_dict: dict[str, Any] = {
            "auth_events": auth_events,
            "prev_events": prev_events,
            "type": self.type,
            "sender": self.sender,
            "content": self.content,
            "unsigned": self.unsigned,
            "depth": depth,
        }
        if self.room_id is not None:
            event_dict["room_id"] = self.room_id

        if self.room_version.msc4291_room_ids_as_hashes:
            # In MSC4291: the create event has no room ID as the create event ID /is/ the room ID.
            if (
                self.type == EventTypes.Create
                and self.is_state()
                and self._state_key == ""
            ):
                assert self.room_id is None
            else:
                # All other events do not reference the create event in auth_events, as the room ID
                # /is/ the create event. However, the rest of the code (for consistency between room
                # versions) assume that the create event remains part of the auth events. c.f. event
                # class which automatically adds the create event when `.auth_event_ids()` is called
                assert self.room_id is not None
                create_event_id = "$" + self.room_id[1:]
                auth_event_ids.remove(create_event_id)
                event_dict["auth_events"] = auth_event_ids

        if self.is_state():
            event_dict["state_key"] = self._state_key

        # MSC2174 moves the redacts property to the content, it is invalid to
        # provide it as a top-level property.
        if self._redacts is not None and not self.room_version.updated_redaction_rules:
            event_dict["redacts"] = self._redacts

        if self._origin_server_ts is not None:
            event_dict["origin_server_ts"] = self._origin_server_ts

        return create_local_event_from_event_dict(
            clock=self._clock,
            hostname=self._hostname,
            signing_key=self._signing_key,
            room_version=self.room_version,
            event_dict=event_dict,
            internal_metadata_dict=self.internal_metadata.get_dict(),
        )


class EventBuilderFactory:
    def __init__(self, hs: "HomeServer"):
        self.clock = hs.get_clock()
        self.hostname = hs.hostname
        self.signing_key = hs.signing_key

        self.store = hs.get_datastores().main
        self.state = hs.get_state_handler()
        self._event_auth_handler = hs.get_event_auth_handler()

    def for_room_version(
        self, room_version: RoomVersion, key_values: dict
    ) -> EventBuilder:
        """Generate an event builder appropriate for the given room version

        Args:
            room_version:
                Version of the room that we're creating an event builder for
            key_values: Fields used as the basis of the new event

        Returns:
            EventBuilder
        """
        return EventBuilder(
            store=self.store,
            state=self.state,
            event_auth_handler=self._event_auth_handler,
            clock=self.clock,
            hostname=self.hostname,
            signing_key=self.signing_key,
            room_version=room_version,
            type=key_values["type"],
            state_key=key_values.get("state_key"),
            room_id=key_values.get("room_id"),
            sender=key_values["sender"],
            content=key_values.get("content", {}),
            unsigned=key_values.get("unsigned", {}),
            redacts=key_values.get("redacts", None),
            origin_server_ts=key_values.get("origin_server_ts", None),
        )


def create_local_event_from_event_dict(
    clock: Clock,
    hostname: str,
    signing_key: SigningKey,
    room_version: RoomVersion,
    event_dict: JsonDict,
    internal_metadata_dict: JsonDict | None = None,
) -> EventBase:
    """Takes a fully formed event dict, ensuring that fields like
    `origin_server_ts` have correct values for a locally produced event,
    then signs and hashes it.
    """

    format_version = room_version.event_format
    if format_version not in KNOWN_EVENT_FORMAT_VERSIONS:
        raise Exception("No event format defined for version %r" % (format_version,))

    if internal_metadata_dict is None:
        internal_metadata_dict = {}

    time_now = int(clock.time_msec())

    if format_version == EventFormatVersions.ROOM_V1_V2:
        event_dict["event_id"] = _create_event_id(clock, hostname)

    event_dict.setdefault("origin_server_ts", time_now)

    event_dict.setdefault("unsigned", {})
    age = event_dict["unsigned"].pop("age", 0)
    event_dict["unsigned"].setdefault("age_ts", time_now - age)

    event_dict.setdefault("signatures", {})

    add_hashes_and_signatures(room_version, event_dict, hostname, signing_key)
    return make_event_from_dict(
        event_dict, room_version, internal_metadata_dict=internal_metadata_dict
    )


# A counter used when generating new event IDs
_event_id_counter = 0


def _create_event_id(clock: Clock, hostname: str) -> str:
    """Create a new event ID

    Args:
        clock
        hostname: The server name for the event ID

    Returns:
        The new event ID
    """

    global _event_id_counter

    i = str(_event_id_counter)
    _event_id_counter += 1

    local_part = str(int(clock.time())) + i + random_string(5)

    e_id = EventID(local_part, hostname)

    return e_id.to_string()
