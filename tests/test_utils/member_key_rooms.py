#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""Helpers for persisting hand-built MSC4430 member key rooms.

Nothing in the server can create or join a member key room yet: the room version
is not registered and the handlers do not translate user IDs to keys. These
helpers build events the way `tests/test_event_auth.py` does (signed by the
sender's member key) and push them straight through the persistence controller,
so that storage can be tested on its own.

Persisting needs the room version to be looked up from `rooms.room_version`, so a
test must register it first (see `register_member_key_room_version`). That
registration is process global and outlives the test.
"""

from typing import Any

from signedjson.key import generate_signing_key

from synapse.api.constants import EventTypes, MemberKeys
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS, RoomVersions
from synapse.crypto.event_signing import (
    EventSigner,
    add_hashes_and_signatures_from_signers,
)
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.server import HomeServer
from synapse.types import JsonDict, MemberKey

MEMBER_KEY_ROOM_VERSION = RoomVersions.MSC4430v12


def register_member_key_room_version() -> None:
    """Make the member key room version known, so that events can be persisted
    into rooms of that version.

    The server itself registers it only once it can handle such rooms; until
    then tests that persist member key events do it themselves. The registry is
    process global, so every later test in the same process sees the version too
    (tests that iterate every known room version with user ID senders are then
    order dependent)."""
    KNOWN_ROOM_VERSIONS.add_room_version(MEMBER_KEY_ROOM_VERSION)


class Member:
    """An identity in a member key room: a signing key and its public form."""

    def __init__(self) -> None:
        self.signing_key = generate_signing_key(MemberKeys.KEY_VERSION)
        self.key = MemberKey.from_signing_key(self.signing_key).to_string()

    @property
    def signer(self) -> EventSigner:
        """Signs as the event's sender."""
        return (MemberKeys.SIGNATURE_ENTITY, self.signing_key)


class MemberKeyRoom:
    """A member key room built one hand-signed event at a time and persisted
    through the persistence controller.

    Events are chained linearly: each new event's `prev_events` is the last
    event persisted into the room's DAG and its `prev_state_events` the last
    state event, with increasing depth. Outliers (out-of-band memberships) are
    persisted outside the chain.

    Usage: `room = MemberKeyRoom(hs, creator)`, then `await room.create()`
    (which stores the `rooms` row and persists the create event), then
    `await room.send(...)` / `await room.send_member(...)`.
    """

    def __init__(self, hs: HomeServer, creator: Member) -> None:
        self._hs = hs
        self._store = hs.get_datastores().main
        self._state_handler = hs.get_state_handler()
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self._persistence = persistence
        self.creator = creator
        self.room_id: str = ""
        self._latest_event_id: str | None = None
        self._latest_state_event_id: str | None = None
        self._depth = 0

    def build(
        self,
        event_dict: JsonDict,
        *signers: EventSigner,
        internal_metadata: dict[str, Any] | None = None,
    ) -> EventBase:
        """Build and sign an event in this room, chained onto the events
        persisted so far unless the dict says otherwise."""
        event_dict = {
            "depth": self._depth + 1,
            "origin_server_ts": self._hs.get_clock().time_msec(),
            "prev_events": (
                [self._latest_event_id] if self._latest_event_id is not None else []
            ),
            "prev_state_events": (
                [self._latest_state_event_id]
                if self._latest_state_event_id is not None
                else []
            ),
            **event_dict,
        }
        if event_dict["type"] != EventTypes.Create:
            assert self.room_id, "create the room first"
            event_dict.setdefault("room_id", self.room_id)
        add_hashes_and_signatures_from_signers(
            MEMBER_KEY_ROOM_VERSION,
            event_dict,
            signers,
            server_name=self._hs.hostname,
        )
        internal_metadata = dict(internal_metadata or {})
        # `outlier` is not part of the internal metadata dict (it has its own
        # column), so it is set on the event afterwards.
        outlier = internal_metadata.pop("outlier", False)
        event = make_event_from_dict(
            event_dict,
            MEMBER_KEY_ROOM_VERSION,
            internal_metadata_dict=internal_metadata,
        )
        if outlier:
            event.internal_metadata.outlier = True
        return event

    def build_member(
        self,
        target: Member,
        membership: str,
        sender: Member | None = None,
        content: JsonDict | None = None,
        internal_metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> EventBase:
        sender = sender or target
        return self.build(
            {
                "type": EventTypes.Member,
                "state_key": target.key,
                "sender": sender.key,
                "content": {"membership": membership, **(content or {})},
                **extra,
            },
            sender.signer,
            internal_metadata=internal_metadata,
        )

    async def persist(self, event: EventBase, backfilled: bool = False) -> EventBase:
        """Persist an event built by `build`, computing its context (or an
        outlier context for outliers), and chain later events onto it."""
        if event.internal_metadata.is_outlier():
            context = EventContext.for_outlier(self._hs.get_storage_controllers())
        else:
            context = await self._state_handler.compute_event_context(event)
            # Events in a state DAG room do not list their auth events; they are
            # calculated from the state before the event and remembered on the
            # event, as the federation handler does for received events.
            event.internal_metadata.calculated_auth_event_ids = (
                self._hs.get_event_auth_handler().compute_auth_events(
                    event, await context.get_prev_state_ids()
                )
            )
        await self._persistence.persist_event(event, context, backfilled=backfilled)
        if not event.internal_metadata.is_outlier() and not backfilled:
            self._latest_event_id = event.event_id
            if event.is_state():
                self._latest_state_event_id = event.event_id
            self._depth = max(self._depth, event.depth)
        return event

    async def persist_batch(self, events: list[EventBase]) -> list[EventBase]:
        """Persist several events built off the same prev events in one batch,
        in the given order (so the first gets the batch's lowest position)."""
        events_and_contexts = []
        for event in events:
            context = await self._state_handler.compute_event_context(event)
            event.internal_metadata.calculated_auth_event_ids = (
                self._hs.get_event_auth_handler().compute_auth_events(
                    event, await context.get_prev_state_ids()
                )
            )
            events_and_contexts.append((event, context))
        persisted, _ = await self._persistence.persist_events(events_and_contexts)
        for event in events:
            self._latest_event_id = event.event_id
            if event.is_state():
                self._latest_state_event_id = event.event_id
            self._depth = max(self._depth, event.depth)
        return persisted

    async def create(self, content: JsonDict | None = None) -> EventBase:
        """Store the `rooms` row and persist the create event, sent by the
        creator's member key. The room ID is the create event's ID."""
        create = self.build(
            {
                "type": EventTypes.Create,
                "state_key": "",
                "sender": self.creator.key,
                "content": {
                    "room_version": MEMBER_KEY_ROOM_VERSION.identifier,
                    **(content or {}),
                },
            },
            self.creator.signer,
        )
        self.room_id = create.room_id
        await self._store.store_room(
            self.room_id,
            room_creator_user_id=self.creator.key,
            is_public=False,
            room_version=MEMBER_KEY_ROOM_VERSION,
        )
        return await self.persist(create)

    async def send(
        self,
        event_dict: JsonDict,
        *signers: EventSigner,
        internal_metadata: dict[str, Any] | None = None,
    ) -> EventBase:
        return await self.persist(
            self.build(event_dict, *signers, internal_metadata=internal_metadata)
        )

    async def send_member(
        self,
        target: Member,
        membership: str,
        sender: Member | None = None,
        content: JsonDict | None = None,
        **extra: Any,
    ) -> EventBase:
        return await self.persist(
            self.build_member(target, membership, sender, content, **extra)
        )

    async def send_message(
        self,
        sender: Member,
        body: str = "hello",
        internal_metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> EventBase:
        return await self.send(
            {
                "type": EventTypes.Message,
                "sender": sender.key,
                "content": {"msgtype": "m.text", "body": body},
                **extra,
            },
            sender.signer,
            internal_metadata=internal_metadata,
        )

    def build_message_at_depth(self, sender: Member, depth: int) -> EventBase:
        """Build, without persisting, a message at the given depth: an event
        "about to be persisted", for exercising extremity pruning directly."""
        return self.build(
            {
                "type": EventTypes.Message,
                "sender": sender.key,
                "content": {"msgtype": "m.text", "body": f"depth {depth}"},
                "depth": depth,
            },
            sender.signer,
        )

    async def send_out_of_band_member(
        self,
        target: Member,
        membership: str,
        sender: Member,
        content: JsonDict | None = None,
        unsigned: JsonDict | None = None,
    ) -> EventBase:
        """Persist an out-of-band membership event (a remote invite, or the
        rejection of one): an outlier, with no place in the room's DAG."""
        event = self.build_member(
            target,
            membership,
            sender,
            content,
            internal_metadata={"outlier": True, "out_of_band_membership": True},
            prev_events=[],
            prev_state_events=[],
            unsigned=unsigned or {},
        )
        return await self.persist(event)
