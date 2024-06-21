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
from typing import Optional
from unittest.mock import patch

from synapse.api.constants import EventUnsignedContentFields
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import create_requester
from synapse.visibility import filter_events_for_client, filter_events_for_server

from tests import unittest
from tests.test_utils.event_injection import inject_event, inject_member_event
from tests.unittest import HomeserverTestCase
from tests.utils import create_room

logger = logging.getLogger(__name__)

TEST_ROOM_ID = "!TEST:ROOM"


class FilterEventsForServerTestCase(unittest.HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.event_creation_handler = self.hs.get_event_creation_handler()
        self.event_builder_factory = self.hs.get_event_builder_factory()
        self._storage_controllers = self.hs.get_storage_controllers()
        assert self._storage_controllers.persistence is not None
        self._persistence = self._storage_controllers.persistence

        self.get_success(create_room(self.hs, TEST_ROOM_ID, "@someone:ROOM"))

    def test_filtering(self) -> None:
        #
        # The events to be filtered consist of 10 membership events (it doesn't
        # really matter if they are joins or leaves, so let's make them joins).
        # One of those membership events is going to be for a user on the
        # server we are filtering for (so we can check the filtering is doing
        # the right thing).
        #

        # before we do that, we persist some other events to act as state.
        self.get_success(
            inject_visibility_event(self.hs, TEST_ROOM_ID, "@admin:hs", "joined")
        )
        for i in range(10):
            self.get_success(
                inject_member_event(
                    self.hs,
                    TEST_ROOM_ID,
                    "@resident%i:hs" % i,
                    "join",
                )
            )

        events_to_filter = []

        for i in range(10):
            evt = self.get_success(
                inject_member_event(
                    self.hs,
                    TEST_ROOM_ID,
                    "@user%i:%s" % (i, "test_server" if i == 5 else "other_server"),
                    "join",
                    extra_content={"a": "b"},
                )
            )
            events_to_filter.append(evt)

        filtered = self.get_success(
            filter_events_for_server(
                self._storage_controllers,
                "test_server",
                "hs",
                events_to_filter,
                redact=True,
                filter_out_erased_senders=True,
                filter_out_remote_partial_state_events=True,
            )
        )

        # the result should be 5 redacted events, and 5 unredacted events.
        for i in range(5):
            self.assertEqual(events_to_filter[i].event_id, filtered[i].event_id)
            self.assertNotIn("a", filtered[i].content)

        for i in range(5, 10):
            self.assertEqual(events_to_filter[i].event_id, filtered[i].event_id)
            self.assertEqual(filtered[i].content["a"], "b")

    def test_filter_outlier(self) -> None:
        # outlier events must be returned, for the good of the collective federation
        self.get_success(
            inject_member_event(
                self.hs,
                TEST_ROOM_ID,
                "@resident:remote_hs",
                "join",
            )
        )
        self.get_success(
            inject_visibility_event(
                self.hs, TEST_ROOM_ID, "@resident:remote_hs", "joined"
            )
        )

        outlier = self._inject_outlier()
        self.assertEqual(
            self.get_success(
                filter_events_for_server(
                    self._storage_controllers,
                    "remote_hs",
                    "hs",
                    [outlier],
                    redact=True,
                    filter_out_erased_senders=True,
                    filter_out_remote_partial_state_events=True,
                )
            ),
            [outlier],
        )

        # it should also work when there are other events in the list
        evt = self.get_success(
            inject_message_event(self.hs, TEST_ROOM_ID, "@unerased:local_hs")
        )

        filtered = self.get_success(
            filter_events_for_server(
                self._storage_controllers,
                "remote_hs",
                "local_hs",
                [outlier, evt],
                redact=True,
                filter_out_erased_senders=True,
                filter_out_remote_partial_state_events=True,
            )
        )
        self.assertEqual(len(filtered), 2, f"expected 2 results, got: {filtered}")
        self.assertEqual(filtered[0], outlier)
        self.assertEqual(filtered[1].event_id, evt.event_id)
        self.assertEqual(filtered[1].content, evt.content)

        # ... but other servers should only be able to see the outlier (the other should
        # be redacted)
        filtered = self.get_success(
            filter_events_for_server(
                self._storage_controllers,
                "other_server",
                "local_hs",
                [outlier, evt],
                redact=True,
                filter_out_erased_senders=True,
                filter_out_remote_partial_state_events=True,
            )
        )
        self.assertEqual(filtered[0], outlier)
        self.assertEqual(filtered[1].event_id, evt.event_id)
        self.assertNotIn("body", filtered[1].content)

    def test_erased_user(self) -> None:
        # 4 message events, from erased and unerased users, with a membership
        # change in the middle of them.
        events_to_filter = []

        evt = self.get_success(
            inject_message_event(self.hs, TEST_ROOM_ID, "@unerased:local_hs")
        )
        events_to_filter.append(evt)

        evt = self.get_success(
            inject_message_event(self.hs, TEST_ROOM_ID, "@erased:local_hs")
        )
        events_to_filter.append(evt)

        evt = self.get_success(
            inject_member_event(
                self.hs,
                TEST_ROOM_ID,
                "@joiner:remote_hs",
                "join",
            )
        )
        events_to_filter.append(evt)

        evt = self.get_success(
            inject_message_event(self.hs, TEST_ROOM_ID, "@unerased:local_hs")
        )
        events_to_filter.append(evt)

        evt = self.get_success(
            inject_message_event(self.hs, TEST_ROOM_ID, "@erased:local_hs")
        )
        events_to_filter.append(evt)

        # the erasey user gets erased
        self.get_success(
            self.hs.get_datastores().main.mark_user_erased("@erased:local_hs")
        )

        # ... and the filtering happens.
        filtered = self.get_success(
            filter_events_for_server(
                self._storage_controllers,
                "test_server",
                "local_hs",
                events_to_filter,
                redact=True,
                filter_out_erased_senders=True,
                filter_out_remote_partial_state_events=True,
            )
        )

        for i in range(len(events_to_filter)):
            self.assertEqual(
                events_to_filter[i].event_id,
                filtered[i].event_id,
                "Unexpected event at result position %i" % (i,),
            )

        for i in (0, 3):
            self.assertEqual(
                events_to_filter[i].content["body"],
                filtered[i].content["body"],
                "Unexpected event content at result position %i" % (i,),
            )

        for i in (1, 4):
            self.assertNotIn("body", filtered[i].content)

    def _inject_outlier(self) -> EventBase:
        builder = self.event_builder_factory.for_room_version(
            RoomVersions.V1,
            {
                "type": "m.room.member",
                "sender": "@test:user",
                "state_key": "@test:user",
                "room_id": TEST_ROOM_ID,
                "content": {"membership": "join"},
            },
        )

        event = self.get_success(builder.build(prev_event_ids=[], auth_event_ids=[]))
        event.internal_metadata.outlier = True
        self.get_success(
            self._persistence.persist_event(
                event, EventContext.for_outlier(self._storage_controllers)
            )
        )
        return event


class FilterEventsForClientTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def test_joined_history_visibility(self) -> None:
        # User joins and leaves room. Should be able to see the join and leave,
        # and messages sent between the two, but not before or after.

        self.register_user("resident", "p1")
        resident_token = self.login("resident", "p1")
        room_id = self.helper.create_room_as("resident", tok=resident_token)

        self.get_success(
            inject_visibility_event(self.hs, room_id, "@resident:test", "joined")
        )
        before_event = self.get_success(
            inject_message_event(self.hs, room_id, "@resident:test", body="before")
        )
        join_event = self.get_success(
            inject_member_event(self.hs, room_id, "@joiner:test", "join")
        )
        during_event = self.get_success(
            inject_message_event(self.hs, room_id, "@resident:test", body="during")
        )
        leave_event = self.get_success(
            inject_member_event(self.hs, room_id, "@joiner:test", "leave")
        )
        after_event = self.get_success(
            inject_message_event(self.hs, room_id, "@resident:test", body="after")
        )

        # We have to reload the events from the db, to ensure that prev_content is
        # populated.
        events_to_filter = [
            self.get_success(
                self.hs.get_storage_controllers().main.get_event(
                    e.event_id,
                    get_prev_content=True,
                )
            )
            for e in [
                before_event,
                join_event,
                during_event,
                leave_event,
                after_event,
            ]
        ]

        # Now run the events through the filter, and check that we can see the events
        # we expect, and that the membership prop is as expected.
        #
        # We deliberately do the queries for both users upfront; this simulates
        # concurrent queries on the server, and helps ensure that we aren't
        # accidentally serving the same event object (with the same unsigned.membership
        # property) to both users.
        joiner_filtered_events = self.get_success(
            filter_events_for_client(
                self.hs.get_storage_controllers(),
                "@joiner:test",
                events_to_filter,
            )
        )
        resident_filtered_events = self.get_success(
            filter_events_for_client(
                self.hs.get_storage_controllers(),
                "@resident:test",
                events_to_filter,
            )
        )

        # The joiner should be able to seem the join and leave,
        # and messages sent between the two, but not before or after.
        self.assertEqual(
            [e.event_id for e in [join_event, during_event, leave_event]],
            [e.event_id for e in joiner_filtered_events],
        )
        self.assertEqual(
            ["join", "join", "leave"],
            [
                e.unsigned[EventUnsignedContentFields.MEMBERSHIP]
                for e in joiner_filtered_events
            ],
        )

        # The resident user should see all the events.
        self.assertEqual(
            [
                e.event_id
                for e in [
                    before_event,
                    join_event,
                    during_event,
                    leave_event,
                    after_event,
                ]
            ],
            [e.event_id for e in resident_filtered_events],
        )
        self.assertEqual(
            ["join", "join", "join", "join", "join"],
            [
                e.unsigned[EventUnsignedContentFields.MEMBERSHIP]
                for e in resident_filtered_events
            ],
        )


class FilterEventsOutOfBandEventsForClientTestCase(
    unittest.FederatingHomeserverTestCase
):
    def test_out_of_band_invite_rejection(self) -> None:
        # this is where we have received an invite event over federation, and then
        # rejected it.
        invite_pdu = {
            "room_id": "!room:id",
            "depth": 1,
            "auth_events": [],
            "prev_events": [],
            "origin_server_ts": 1,
            "sender": "@someone:" + self.OTHER_SERVER_NAME,
            "type": "m.room.member",
            "state_key": "@user:test",
            "content": {"membership": "invite"},
        }
        self.add_hashes_and_signatures_from_other_server(invite_pdu)
        invite_event_id = make_event_from_dict(invite_pdu, RoomVersions.V9).event_id

        self.get_success(
            self.hs.get_federation_server().on_invite_request(
                self.OTHER_SERVER_NAME,
                invite_pdu,
                "9",
            )
        )

        # stub out do_remotely_reject_invite so that we fall back to a locally-
        # generated rejection
        with patch.object(
            self.hs.get_federation_handler(),
            "do_remotely_reject_invite",
            side_effect=Exception(),
        ):
            reject_event_id, _ = self.get_success(
                self.hs.get_room_member_handler().remote_reject_invite(
                    invite_event_id,
                    txn_id=None,
                    requester=create_requester("@user:test"),
                    content={},
                )
            )

        invite_event, reject_event = self.get_success(
            self.hs.get_datastores().main.get_events_as_list(
                [invite_event_id, reject_event_id]
            )
        )

        # the invited user should be able to see both the invite and the rejection
        filtered_events = self.get_success(
            filter_events_for_client(
                self.hs.get_storage_controllers(),
                "@user:test",
                [invite_event, reject_event],
            )
        )
        self.assertEqual(
            [e.event_id for e in filtered_events],
            [e.event_id for e in [invite_event, reject_event]],
        )
        self.assertEqual(
            ["invite", "leave"],
            [
                e.unsigned[EventUnsignedContentFields.MEMBERSHIP]
                for e in filtered_events
            ],
        )

        # other users should see neither
        self.assertEqual(
            self.get_success(
                filter_events_for_client(
                    self.hs.get_storage_controllers(),
                    "@other:test",
                    [invite_event, reject_event],
                )
            ),
            [],
        )


async def inject_visibility_event(
    hs: HomeServer,
    room_id: str,
    sender: str,
    visibility: str,
) -> EventBase:
    return await inject_event(
        hs,
        type="m.room.history_visibility",
        sender=sender,
        state_key="",
        room_id=room_id,
        content={"history_visibility": visibility},
    )


async def inject_message_event(
    hs: HomeServer,
    room_id: str,
    sender: str,
    body: Optional[str] = "testytest",
) -> EventBase:
    return await inject_event(
        hs,
        type="m.room.message",
        sender=sender,
        room_id=room_id,
        content={"body": body, "msgtype": "m.text"},
    )
