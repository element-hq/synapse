#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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
from typing import List, Optional, cast

from canonicaljson import json

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, Membership
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.events.builder import EventBuilder
from synapse.server import HomeServer
from synapse.synapse_rust.events import EventInternalMetadata
from synapse.types import JsonDict, RoomID, UserID
from synapse.util import Clock

from tests import unittest
from tests.utils import create_room


class RedactionTestCase(unittest.HomeserverTestCase):
    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["redaction_retention_period"] = "30d"
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        storage = hs.get_storage_controllers()
        assert storage.persistence is not None
        self._persistence = storage.persistence
        self.event_builder_factory = hs.get_event_builder_factory()
        self.event_creation_handler = hs.get_event_creation_handler()

        self.u_alice = UserID.from_string("@alice:test")
        self.u_bob = UserID.from_string("@bob:test")

        self.room1 = RoomID.from_string("!abc123:test")

        self.get_success(
            create_room(hs, self.room1.to_string(), self.u_alice.to_string())
        )

        self.depth = 1

    def inject_room_member(  # type: ignore[override]
        self,
        room: RoomID,
        user: UserID,
        membership: str,
        extra_content: Optional[JsonDict] = None,
    ) -> EventBase:
        content = {"membership": membership}
        content.update(extra_content or {})
        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Member,
                "sender": user.to_string(),
                "state_key": user.to_string(),
                "room_id": room.to_string(),
                "content": content,
            },
        )

        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        context = self.get_success(unpersisted_context.persist(event))

        self.get_success(self._persistence.persist_event(event, context))

        return event

    def inject_message(self, room: RoomID, user: UserID, body: str) -> EventBase:
        self.depth += 1

        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Message,
                "sender": user.to_string(),
                "state_key": user.to_string(),
                "room_id": room.to_string(),
                "content": {"body": body, "msgtype": "message"},
            },
        )

        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        context = self.get_success(unpersisted_context.persist(event))

        self.get_success(self._persistence.persist_event(event, context))

        return event

    def inject_redaction(
        self, room: RoomID, event_id: str, user: UserID, reason: str
    ) -> EventBase:
        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Redaction,
                "sender": user.to_string(),
                "state_key": user.to_string(),
                "room_id": room.to_string(),
                "content": {"reason": reason},
                "redacts": event_id,
            },
        )

        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        context = self.get_success(unpersisted_context.persist(event))

        self.get_success(self._persistence.persist_event(event, context))

        return event

    def test_redact(self) -> None:
        self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)

        msg_event = self.inject_message(self.room1, self.u_alice, "t")

        # Check event has not been redacted:
        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {"body": "t", "msgtype": "message"},
            },
            event,
        )

        self.assertFalse("redacted_because" in event.unsigned)

        # Redact event
        reason = "Because I said so"
        self.inject_redaction(self.room1, msg_event.event_id, self.u_alice, reason)

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertEqual(msg_event.event_id, event.event_id)

        self.assertTrue("redacted_because" in event.unsigned)

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {},
            },
            event,
        )

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Redaction,
                "user_id": self.u_alice.to_string(),
                "content": {"reason": reason},
            },
            event.unsigned["redacted_because"],
        )

    def test_redact_join(self) -> None:
        self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)

        msg_event = self.inject_room_member(
            self.room1, self.u_bob, Membership.JOIN, extra_content={"blue": "red"}
        )

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Member,
                "user_id": self.u_bob.to_string(),
                "content": {"membership": Membership.JOIN, "blue": "red"},
            },
            event,
        )

        self.assertFalse(hasattr(event, "redacted_because"))

        # Redact event
        reason = "Because I said so"
        self.inject_redaction(self.room1, msg_event.event_id, self.u_alice, reason)

        # Check redaction

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertTrue("redacted_because" in event.unsigned)

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Member,
                "user_id": self.u_bob.to_string(),
                "content": {"membership": Membership.JOIN},
            },
            event,
        )

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Redaction,
                "user_id": self.u_alice.to_string(),
                "content": {"reason": reason},
            },
            event.unsigned["redacted_because"],
        )

    def test_circular_redaction(self) -> None:
        redaction_event_id1 = "$redaction1_id:test"
        redaction_event_id2 = "$redaction2_id:test"

        class EventIdManglingBuilder:
            def __init__(self, base_builder: EventBuilder, event_id: str):
                self._base_builder = base_builder
                self._event_id = event_id

            async def build(
                self,
                prev_event_ids: List[str],
                auth_event_ids: Optional[List[str]],
                depth: Optional[int] = None,
            ) -> EventBase:
                built_event = await self._base_builder.build(
                    prev_event_ids=prev_event_ids, auth_event_ids=auth_event_ids
                )

                built_event._event_id = self._event_id  # type: ignore[attr-defined]
                built_event._dict["event_id"] = self._event_id
                assert built_event.event_id == self._event_id

                return built_event

            @property
            def room_id(self) -> str:
                return self._base_builder.room_id

            @property
            def type(self) -> str:
                return self._base_builder.type

            @property
            def internal_metadata(self) -> EventInternalMetadata:
                return self._base_builder.internal_metadata

        event_1, unpersisted_context_1 = self.get_success(
            self.event_creation_handler.create_new_client_event(
                cast(
                    EventBuilder,
                    EventIdManglingBuilder(
                        self.event_builder_factory.for_room_version(
                            RoomVersions.V1,
                            {
                                "type": EventTypes.Redaction,
                                "sender": self.u_alice.to_string(),
                                "room_id": self.room1.to_string(),
                                "content": {"reason": "test"},
                                "redacts": redaction_event_id2,
                            },
                        ),
                        redaction_event_id1,
                    ),
                )
            )
        )

        context_1 = self.get_success(unpersisted_context_1.persist(event_1))

        self.get_success(self._persistence.persist_event(event_1, context_1))

        event_2, unpersisted_context_2 = self.get_success(
            self.event_creation_handler.create_new_client_event(
                cast(
                    EventBuilder,
                    EventIdManglingBuilder(
                        self.event_builder_factory.for_room_version(
                            RoomVersions.V1,
                            {
                                "type": EventTypes.Redaction,
                                "sender": self.u_alice.to_string(),
                                "room_id": self.room1.to_string(),
                                "content": {"reason": "test"},
                                "redacts": redaction_event_id1,
                            },
                        ),
                        redaction_event_id2,
                    ),
                )
            )
        )

        context_2 = self.get_success(unpersisted_context_2.persist(event_2))
        self.get_success(self._persistence.persist_event(event_2, context_2))

        # fetch one of the redactions
        fetched = self.get_success(self.store.get_event(redaction_event_id1))

        # it should have been redacted
        self.assertEqual(fetched.unsigned["redacted_by"], redaction_event_id2)
        self.assertEqual(
            fetched.unsigned["redacted_because"].event_id, redaction_event_id2
        )

    def test_redact_censor(self) -> None:
        """Test that a redacted event gets censored in the DB after a month"""

        self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)

        msg_event = self.inject_message(self.room1, self.u_alice, "t")

        # Check event has not been redacted:
        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {"body": "t", "msgtype": "message"},
            },
            event,
        )

        self.assertFalse("redacted_because" in event.unsigned)

        # Redact event
        reason = "Because I said so"
        self.inject_redaction(self.room1, msg_event.event_id, self.u_alice, reason)

        event = self.get_success(self.store.get_event(msg_event.event_id))

        self.assertTrue("redacted_because" in event.unsigned)

        self.assertObjectHasAttributes(
            {
                "type": EventTypes.Message,
                "user_id": self.u_alice.to_string(),
                "content": {},
            },
            event,
        )

        event_json = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="event_json",
                keyvalues={"event_id": msg_event.event_id},
                retcol="json",
            )
        )

        self.assert_dict(
            {"content": {"body": "t", "msgtype": "message"}}, json.loads(event_json)
        )

        # Advance by 30 days, then advance again to ensure that the looping call
        # for updating the stream position gets called and then the looping call
        # for the censoring gets called.
        self.reactor.advance(60 * 60 * 24 * 31)
        self.reactor.advance(60 * 60 * 2)

        event_json = self.get_success(
            self.store.db_pool.simple_select_one_onecol(
                table="event_json",
                keyvalues={"event_id": msg_event.event_id},
                retcol="json",
            )
        )

        self.assert_dict({"content": {}}, json.loads(event_json))

    def test_redact_redaction(self) -> None:
        """Tests that we can redact a redaction and can fetch it again."""

        self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)

        msg_event = self.inject_message(self.room1, self.u_alice, "t")

        first_redact_event = self.inject_redaction(
            self.room1, msg_event.event_id, self.u_alice, "Redacting message"
        )

        self.inject_redaction(
            self.room1,
            first_redact_event.event_id,
            self.u_alice,
            "Redacting redaction",
        )

        # Now lets jump to the future where we have censored the redaction event
        # in the DB.
        self.reactor.advance(60 * 60 * 24 * 31)

        # We just want to check that fetching the event doesn't raise an exception.
        self.get_success(
            self.store.get_event(first_redact_event.event_id, allow_none=True)
        )

    def test_store_redacted_redaction(self) -> None:
        """Tests that we can store a redacted redaction."""

        self.inject_room_member(self.room1, self.u_alice, Membership.JOIN)

        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": EventTypes.Redaction,
                "sender": self.u_alice.to_string(),
                "room_id": self.room1.to_string(),
                "content": {"reason": "foo"},
            },
        )

        redaction_event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_new_client_event(builder)
        )

        context = self.get_success(unpersisted_context.persist(redaction_event))

        self.get_success(self._persistence.persist_event(redaction_event, context))

        # Now lets jump to the future where we have censored the redaction event
        # in the DB.
        self.reactor.advance(60 * 60 * 24 * 31)

        # We just want to check that fetching the event doesn't raise an exception.
        self.get_success(
            self.store.get_event(redaction_event.event_id, allow_none=True)
        )
