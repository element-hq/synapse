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
"""The redaction "recheck".

In room versions since v3, a redaction by a sender without the power to redact
other people's events is still applied if both senders are from the same server.
The auth rules cannot decide that when the redacted event is not yet known, so
`check_redaction` marks the redaction for a recheck, which is then made in three
places: when events are read from the cache or database (where a redaction that
fails it is withheld), when a redaction is applied to the event it redacts on
read, and when an event is persisted after a redaction of it already was. These
tests exercise all three with events injected in the state the auth rules leave
them in.
"""

from twisted.internet.testing import MemoryReactor

from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_injection import create_event, inject_member_event


class RedactionRecheckTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence

        self.alice = self.register_user("alice", "pass")
        self.room_id = self.helper.create_room_as(
            self.alice, room_version="12", tok=self.login("alice", "pass")
        )
        # Another user of ours, and a user of another server, both without the
        # power to redact.
        self.bob = self.register_user("bob", "pass")
        self.helper.join(self.room_id, self.bob, tok=self.login("bob", "pass"))
        self.mallory = "@mallory:other.example"
        self.get_success(
            inject_member_event(self.hs, self.room_id, self.mallory, "join")
        )

    def _message(self) -> tuple[EventBase, EventContext]:
        return self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.room.message",
                sender=self.alice,
                content={"msgtype": "m.text", "body": "hello"},
            )
        )

    def _redaction(self, sender: str, redacts: str) -> tuple[EventBase, EventContext]:
        """A redaction as the auth rules leave one from a sender without redact
        power: to be rechecked against the redacted event's sender."""
        return self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.room.redaction",
                sender=sender,
                content={"redacts": redacts},
                internal_metadata={"recheck_redaction": True},
            )
        )

    def _persist(self, event: EventBase, context: EventContext) -> None:
        self.get_success(self.persistence.persist_event(event, context))

    def _fetch(self, event_id: str) -> EventBase | None:
        return self.get_success(self.store.get_event(event_id, allow_none=True))

    def test_applied_on_read_for_same_server(self) -> None:
        message, context = self._message()
        self._persist(message, context)
        redaction, context = self._redaction(self.bob, message.event_id)
        self._persist(redaction, context)

        fetched = self._fetch(message.event_id)
        assert fetched is not None
        self.assertEqual(fetched.internal_metadata.redacted_by, redaction.event_id)
        self.assertNotIn("body", fetched.content)
        # The redaction itself is served.
        self.assertIsNotNone(self._fetch(redaction.event_id))

    def test_withheld_on_read_for_other_server(self) -> None:
        message, context = self._message()
        self._persist(message, context)
        redaction, context = self._redaction(self.mallory, message.event_id)
        self._persist(redaction, context)

        fetched = self._fetch(message.event_id)
        assert fetched is not None
        self.assertIsNone(fetched.internal_metadata.redacted_by)
        self.assertEqual(fetched.content["body"], "hello")
        # A redaction that fails the recheck is withheld entirely.
        self.assertIsNone(self._fetch(redaction.event_id))

    def test_applied_at_persistence_for_same_server(self) -> None:
        """The redaction arrives before the event it redacts."""
        message, message_context = self._message()
        redaction, context = self._redaction(self.bob, message.event_id)
        self._persist(redaction, context)
        self._persist(message, message_context)

        fetched = self._fetch(message.event_id)
        assert fetched is not None
        self.assertEqual(fetched.internal_metadata.redacted_by, redaction.event_id)
        self.assertNotIn("body", fetched.content)

    def test_skipped_at_persistence_for_other_server(self) -> None:
        message, message_context = self._message()
        redaction, context = self._redaction(self.mallory, message.event_id)
        self._persist(redaction, context)
        self._persist(message, message_context)

        fetched = self._fetch(message.event_id)
        assert fetched is not None
        self.assertIsNone(fetched.internal_metadata.redacted_by)
        self.assertEqual(fetched.content["body"], "hello")
