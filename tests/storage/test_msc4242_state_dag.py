#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import Iterable
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.api.errors import SynapseError
from synapse.api.room_versions import RoomVersions
from synapse.events import FrozenEventVMSC4242, make_event_from_dict
from synapse.events.snapshot import EventContext
from synapse.rest.client import room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class MSC4242StateDagsTests(HomeserverTestCase):
    user_id = "@user1:server"
    servlets = [room.register_servlets]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver("server")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.room_id = self.helper.create_room_as(
            self.user_id,
            room_version=RoomVersions.MSC4242v12.identifier,
        )

        self.store = hs.get_datastores().main
        self._storage_controllers = self.hs.get_storage_controllers()

    def _get_prev_state_events(self, event_id: str) -> list[str]:
        ev = self.helper.get_event(self.room_id, event_id)
        prev_state_events: list[str] | None = ev.get("prev_state_events", None)
        assert prev_state_events is not None
        return prev_state_events

    def test_forward_extremities_are_calculated(self) -> None:
        """
        Check that forward extremities are set as prev_state_events and that they don't change
        for non-state events.
        """
        # they don't change for messages
        first_id = self.helper.send(self.room_id, body="test1")["event_id"]
        first_pae = self._get_prev_state_events(first_id)
        assert len(first_pae) == 1
        second_id = self.helper.send(self.room_id, body="test2")["event_id"]
        second_pae = self._get_prev_state_events(second_id)
        assert len(second_pae) == 1
        self.assertEquals(first_pae, second_pae)

        # send another auth event, which should change the prev_state_events on subsequent events
        jr_id = self.helper.send_state(
            self.room_id,
            EventTypes.JoinRules,
            {
                "join_rule": "knock",
            },
            tok="nope",
        )["event_id"]
        jr_pae = self._get_prev_state_events(jr_id)
        self.assertEquals(second_pae, jr_pae)

        # prev_state_events should always point to the join rule now
        third_id = self.helper.send(self.room_id, body="test3")["event_id"]
        third_pae = self._get_prev_state_events(third_id)
        self.assertEquals(third_pae, [jr_id])
        # ..including for non-auth state
        # TODO FIXME KEGAN
        # name_id = self.helper.send_state(
        #    self.room_id,
        #    EventTypes.Name,
        #    {
        #        "name": "State DAGs!",
        #    },
        #    tok="nope",
        # )["event_id"]
        # name_pae = self._get_prev_state_events(name_id)
        # self.assertEquals(name_pae, [jr_id])


class MSC4242EventPersistenceAuthDagsStoreTestCase(HomeserverTestCase):
    servlets = [
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence
        self.room_id = "!foo:bar"
        self.seen_event_ids: set[str] = set()
        self.persistence.main_store = Mock(spec=["have_seen_events"])
        self.persistence.main_store.have_seen_events.side_effect = (
            self._have_seen_events
        )
        self.rejected_event_ids_and_their_prevs: set[str] = set()
        self.persistence.persist_events_store = Mock(
            spec=["_get_prevs_before_rejected"]
        )
        self.persistence.persist_events_store._get_prevs_before_rejected.side_effect = (
            self._get_prevs_before_rejected
        )

    async def _have_seen_events(
        self, room_id: str, event_ids: Iterable[str]
    ) -> set[str]:
        unknown_events = set(event_ids)
        return self.seen_event_ids.intersection(unknown_events)

    async def _get_prevs_before_rejected(
        self, event_ids: Iterable[str], include_soft_failed: bool = True
    ) -> set[str]:
        return self.rejected_event_ids_and_their_prevs

    def _make_event(
        self,
        id: str,
        prev_state_events: list[str],
        rejected: bool = False,
    ) -> tuple[FrozenEventVMSC4242, EventContext]:
        ev = make_event_from_dict(
            {
                "prev_state_events": prev_state_events,
                "content": {
                    "membership": "join",
                },
                "sender": "@unimportant:info",
                "state_key": "@unimportant:info",
                "type": "m.room.member",
                "room_id": self.room_id,
            },
            room_version=RoomVersions.MSC4242v12,
        )
        assert isinstance(ev, FrozenEventVMSC4242)
        ev._event_id = id
        ctx = Mock()
        ctx.rejected = rejected
        return ev, ctx

    def _test(
        self,
        current_fwds: list[str],
        new_events: list[tuple[FrozenEventVMSC4242, EventContext]],
        want_new_extrems: set[str],
        want_raises: bool = False,
    ) -> None:
        promise = self.persistence._calculate_new_state_dag_extremities(
            self.room_id,
            frozenset(current_fwds),
            new_events,
        )
        if want_raises:
            f = self.get_failure(promise, SynapseError)
            assert f is not None
            return

        new_extrems = set(self.get_success(promise))
        self.assertEqual(
            new_extrems,
            want_new_extrems,
            f"want_new_extrems={want_new_extrems} got={new_extrems}",
        )

    def test_calculate_new_state_dag_extremities_simple(self) -> None:
        # Simple linear chain
        self._test(
            current_fwds=[],
            new_events=[
                self._make_event("$1", []),
                self._make_event("$2", ["$1"]),
                self._make_event("$3", ["$2"]),
                self._make_event("$4", ["$3"]),
            ],
            want_new_extrems={"$4"},
        )

    def test_calculate_new_state_dag_extremities_fork(self) -> None:
        # Simple fork so we end up with two forward extrems
        self._test(
            current_fwds=[],
            new_events=[
                self._make_event("$1", []),
                self._make_event("$2", ["$1"]),
                self._make_event("$3", ["$2"]),
                self._make_event("$4", ["$2"]),
            ],
            want_new_extrems={"$3", "$4"},
        )

    def test_calculate_new_state_dag_extremities_merge(self) -> None:
        # Simple fork so we end up with two forward extrems
        self._test(
            current_fwds=[],
            new_events=[
                self._make_event("$1", []),
                self._make_event("$2", ["$1"]),
                self._make_event("$3", ["$1"]),
                self._make_event("$4", ["$2", "$3"]),
            ],
            want_new_extrems={"$4"},
        )

    def test_calculate_new_state_dag_extremities_fork_on_existing(self) -> None:
        # Fork where we are adding to older events
        self.seen_event_ids = {"$1", "$2", "$3"}
        self._test(
            current_fwds=["$3"],
            new_events=[
                self._make_event("$4", ["$3"]),  # append to the forward extrem
                self._make_event("$5", ["$1"]),  # append to the root
            ],
            want_new_extrems={"$4", "$5"},
        )

    def test_calculate_new_state_dag_extremities_merge_on_existing(self) -> None:
        # Merge where we are merging to older events
        self.seen_event_ids = {"$1", "$2", "$3"}
        self._test(
            current_fwds=["$3"],
            new_events=[
                self._make_event("$4", ["$3", "$2"]),
            ],
            want_new_extrems={"$4"},
        )

    def test_calculate_new_state_dag_extremities_merge_on_not_current(self) -> None:
        # Merge where we are merging to older events
        self.seen_event_ids = {"$1", "$2", "$3"}
        self._test(
            current_fwds=["$3"],
            new_events=[
                self._make_event("$4", ["$1", "$2"]),
            ],
            want_new_extrems={"$3", "$4"},
        )

    def test_calculate_new_state_dag_extremities_append_with_rejected(self) -> None:
        # rejected events cannot be forward extremities
        self.seen_event_ids = {"$1", "$2", "$3"}
        self._test(
            current_fwds=["$3"],
            new_events=[
                self._make_event("$4", ["$3"], rejected=True),
            ],
            want_new_extrems={"$3"},
        )

        self._test(
            current_fwds=["$3"],
            new_events=[
                self._make_event("$4", ["$3"], rejected=True),
                self._make_event("$5", ["$4"], rejected=True),
            ],
            want_new_extrems={"$3"},
        )

    def test_calculate_new_state_dag_extremities_append_with_rejected_in_chain(
        self,
    ) -> None:
        # rejected events cannot be forward extremities, but events that come after them can.
        # this shouldn't cause multiple forward extremities.
        self.seen_event_ids = {"$1", "$2", "$3"}
        self.rejected_event_ids_and_their_prevs = {"$4", "$3"}
        self._test(
            current_fwds=["$3"],
            new_events=[
                self._make_event("$4", ["$3"], rejected=True),
                self._make_event("$5", ["$4"]),
            ],
            want_new_extrems={"$5"},
        )

    def test_calculate_new_state_dag_extremities_missing_prevs_raises(self) -> None:
        self._test(
            current_fwds=[],
            new_events=[
                self._make_event("$1", []),
                self._make_event("$2", ["$1"]),
                self._make_event("$3", ["$unknown"]),
                self._make_event("$4", ["$3"]),
            ],
            want_new_extrems={"$4"},
            want_raises=True,
        )

    def test_calculate_new_state_dag_extremities_complex(self) -> None:
        """
            1
            | \
            2  4
            |
            3

            Exists already, then becomes...

            1______
            | \\   |
            2  4  5R
            |  |  |
            3--7  6R
            |  \\ /  \
           10R  8   9

        """
        # Merge where we are merging to older events
        self.seen_event_ids = {"$1", "$2", "$3", "$4"}
        self.rejected_event_ids_and_their_prevs = {"$1", "$5", "$6", "$3", "$10"}
        self._test(
            current_fwds=["$3", "$4"],
            new_events=[
                self._make_event("$5", ["$1"], rejected=True),
                self._make_event("$6", ["$5"], rejected=True),
                self._make_event("$7", ["$4", "$3"]),
                self._make_event("$8", ["$6", "$7"]),
                self._make_event("$9", ["$6"]),
                self._make_event("$10", ["$3"], rejected=True),
            ],
            want_new_extrems={"$8", "$9"},
        )
