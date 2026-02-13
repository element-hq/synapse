#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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
from typing import Any, Iterable

from canonicaljson import encode_canonical_json
from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import ReceiptTypes
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.server import HomeServer
from synapse.storage.databases.main.event_push_actions import (
    NotifCounts,
    RoomNotifCounts,
)
from synapse.storage.databases.main.events_worker import EventsWorkerStore
from synapse.storage.roommember import RoomsForUser
from synapse.types import PersistedEventPosition
from synapse.util.clock import Clock

from ._base import BaseWorkerStoreTestCase

USER_ID = "@feeling:test"
USER_ID_2 = "@bright:test"
OUTLIER = {"outlier": True}
ROOM_ID = "!room:test"

logger = logging.getLogger(__name__)


class EventsWorkerStoreTestCase(BaseWorkerStoreTestCase):
    STORE_TYPE = EventsWorkerStore

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.get_success(
            self.master_store.store_room(
                ROOM_ID,
                USER_ID,
                is_public=False,
                room_version=RoomVersions.V1,
            )
        )

    def assertEventsEqual(
        self, first: EventBase, second: EventBase, msg: Any | None = None
    ) -> None:
        self.assertEqual(
            encode_canonical_json(first.get_pdu_json()),
            encode_canonical_json(second.get_pdu_json()),
            msg,
        )

    def test_get_latest_event_ids_in_room(self) -> None:
        create = self.persist(type="m.room.create", key="", creator=USER_ID)
        self.replicate()
        self.check("get_latest_event_ids_in_room", (ROOM_ID,), {create.event_id})

        join = self.persist(
            type="m.room.member",
            key=USER_ID,
            membership="join",
            prev_events=[(create.event_id, {})],
        )
        self.replicate()
        self.check("get_latest_event_ids_in_room", (ROOM_ID,), {join.event_id})

    def test_redactions(self) -> None:
        self.persist(type="m.room.create", key="", creator=USER_ID)
        self.persist(type="m.room.member", key=USER_ID, membership="join")

        msg = self.persist(type="m.room.message", msgtype="m.text", body="Hello")
        self.replicate()
        self.check("get_event", [msg.event_id], msg, asserter=self.assertEventsEqual)

        redaction = self.persist(type="m.room.redaction", redacts=msg.event_id)
        self.replicate()

        msg_dict = msg.get_dict()
        msg_dict["content"] = {}
        msg_dict["unsigned"]["redacted_by"] = redaction.event_id
        msg_dict["unsigned"]["redacted_because"] = redaction
        redacted = make_event_from_dict(
            msg_dict, internal_metadata_dict=msg.internal_metadata.get_dict()
        )
        self.check(
            "get_event", [msg.event_id], redacted, asserter=self.assertEventsEqual
        )

    def test_backfilled_redactions(self) -> None:
        self.persist(type="m.room.create", key="", creator=USER_ID)
        self.persist(type="m.room.member", key=USER_ID, membership="join")

        msg = self.persist(type="m.room.message", msgtype="m.text", body="Hello")
        self.replicate()
        self.check("get_event", [msg.event_id], msg, asserter=self.assertEventsEqual)

        redaction = self.persist(
            type="m.room.redaction", redacts=msg.event_id, backfill=True
        )
        self.replicate()

        msg_dict = msg.get_dict()
        msg_dict["content"] = {}
        msg_dict["unsigned"]["redacted_by"] = redaction.event_id
        msg_dict["unsigned"]["redacted_because"] = redaction
        redacted = make_event_from_dict(
            msg_dict, internal_metadata_dict=msg.internal_metadata.get_dict()
        )
        self.check(
            "get_event", [msg.event_id], redacted, asserter=self.assertEventsEqual
        )

    def test_invites(self) -> None:
        self.persist(type="m.room.create", key="", creator=USER_ID)
        self.check("get_invited_rooms_for_local_user", [USER_ID_2], [])
        event = self.persist(type="m.room.member", key=USER_ID_2, membership="invite")
        assert event.internal_metadata.instance_name is not None
        assert event.internal_metadata.stream_ordering is not None

        self.replicate()

        self.check(
            "get_invited_rooms_for_local_user",
            [USER_ID_2],
            [
                RoomsForUser(
                    ROOM_ID,
                    USER_ID,
                    "invite",
                    event.event_id,
                    PersistedEventPosition(
                        event.internal_metadata.instance_name,
                        event.internal_metadata.stream_ordering,
                    ),
                    RoomVersions.V1.identifier,
                )
            ],
        )

    @parameterized.expand([(True,), (False,)])
    def test_push_actions_for_user(self, send_receipt: bool) -> None:
        self.persist(type="m.room.create", key="", creator=USER_ID)
        self.persist(type="m.room.member", key=USER_ID, membership="join")
        self.persist(
            type="m.room.member", sender=USER_ID, key=USER_ID_2, membership="join"
        )
        event1 = self.persist(type="m.room.message", msgtype="m.text", body="hello")
        self.replicate()

        if send_receipt:
            self.get_success(
                self.master_store.insert_receipt(
                    ROOM_ID, ReceiptTypes.READ, USER_ID_2, [event1.event_id], None, {}
                )
            )

        self.check(
            "get_unread_event_push_actions_by_room_for_user",
            [ROOM_ID, USER_ID_2],
            RoomNotifCounts(
                NotifCounts(highlight_count=0, unread_count=0, notify_count=0), {}
            ),
        )

        self.persist(
            type="m.room.message",
            msgtype="m.text",
            body="world",
            push_actions=[(USER_ID_2, ["notify"])],
        )
        self.replicate()
        self.check(
            "get_unread_event_push_actions_by_room_for_user",
            [ROOM_ID, USER_ID_2],
            RoomNotifCounts(
                NotifCounts(highlight_count=0, unread_count=0, notify_count=1), {}
            ),
        )

        self.persist(
            type="m.room.message",
            msgtype="m.text",
            body="world",
            push_actions=[
                (USER_ID_2, ["notify", {"set_tweak": "highlight", "value": True}])
            ],
        )
        self.replicate()
        self.check(
            "get_unread_event_push_actions_by_room_for_user",
            [ROOM_ID, USER_ID_2],
            RoomNotifCounts(
                NotifCounts(highlight_count=1, unread_count=0, notify_count=2), {}
            ),
        )

    event_id = 0

    def persist(self, backfill: bool = False, **kwargs: Any) -> EventBase:
        """
        Returns:
            The event that was persisted.
        """
        event, context = self.build_event(**kwargs)

        if backfill:
            self.get_success(
                self.persistance.persist_events([(event, context)], backfilled=True)
            )
        else:
            self.get_success(self.persistance.persist_event(event, context))

        return event

    def build_event(
        self,
        sender: str = USER_ID,
        room_id: str = ROOM_ID,
        type: str = "m.room.message",
        key: str | None = None,
        internal: dict | None = None,
        depth: int | None = None,
        prev_events: list[tuple[str, dict]] | None = None,
        auth_events: list[str] | None = None,
        prev_state: list[str] | None = None,
        redacts: str | None = None,
        push_actions: Iterable = frozenset(),
        **content: object,
    ) -> tuple[EventBase, EventContext]:
        prev_events = prev_events or []
        auth_events = auth_events or []
        prev_state = prev_state or []

        if depth is None:
            depth = self.event_id

        if not prev_events:
            latest_event_ids = self.get_success(
                self.master_store.get_latest_event_ids_in_room(room_id)
            )
            prev_events = [(ev_id, {}) for ev_id in latest_event_ids]

        event_dict = {
            "sender": sender,
            "type": type,
            "content": content,
            "event_id": "$%d:blue" % (self.event_id,),
            "room_id": room_id,
            "depth": depth,
            "origin_server_ts": self.event_id,
            "prev_events": prev_events,
            "auth_events": auth_events,
        }
        if key is not None:
            event_dict["state_key"] = key
            event_dict["prev_state"] = prev_state

        if redacts is not None:
            event_dict["redacts"] = redacts

        event = make_event_from_dict(event_dict, internal_metadata_dict=internal or {})

        self.event_id += 1
        state_handler = self.hs.get_state_handler()
        context = self.get_success(state_handler.compute_event_context(event))

        self.get_success(
            self.master_store.add_push_actions_to_staging(
                event.event_id,
                dict(push_actions),
                False,
                "main",
            )
        )
        return event, context
