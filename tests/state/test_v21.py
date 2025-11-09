#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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
import itertools
from typing import Sequence

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, JoinRules, Membership
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.federation.federation_base import event_from_pdu_json
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.state import StateResolutionStore
from synapse.state.v2 import (
    StateResolutionStore as StateResolutionStoreInterface,
    _get_auth_chain_difference,
    _seperate,
    resolve_events_with_store,
)
from synapse.types import StateMap
from synapse.util.clock import Clock

from tests import unittest
from tests.state.test_v2 import TestStateResolutionStore

ALICE = "@alice:example.com"
BOB = "@bob:example.com"
CHARLIE = "@charlie:example.com"
EVELYN = "@evelyn:example.com"
ZARA = "@zara:example.com"

ROOM_ID = "!test:example.com"

MEMBERSHIP_CONTENT_JOIN = {"membership": Membership.JOIN}
MEMBERSHIP_CONTENT_INVITE = {"membership": Membership.INVITE}
MEMBERSHIP_CONTENT_LEAVE = {"membership": Membership.LEAVE}


ORIGIN_SERVER_TS = 0


def monotonic_timestamp() -> int:
    global ORIGIN_SERVER_TS
    ORIGIN_SERVER_TS += 1
    return ORIGIN_SERVER_TS


class FakeClock:
    async def sleep(self, duration_ms: float) -> None:
        defer.succeed(None)


class StateResV21TestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.state = self.hs.get_state_handler()
        persistence = self.hs.get_storage_controllers().persistence
        assert persistence is not None
        self._persistence = persistence
        self._state_storage_controller = self.hs.get_storage_controllers().state
        self._state_deletion = self.hs.get_datastores().state_deletion
        self.store = self.hs.get_datastores().main

        self.register_user("user", "pass")
        self.token = self.login("user", "pass")

    def test_state_reset_replay_conflicted_subgraph(self) -> None:
        # 1. Alice creates a room.
        e1_create = self.create_event(
            EventTypes.Create,
            "",
            sender=ALICE,
            content={"creator": ALICE},
            auth_events=[],
        )
        # 2. Alice joins it.
        e2_ma = self.create_event(
            EventTypes.Member,
            ALICE,
            sender=ALICE,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[],
            prev_events=[e1_create.event_id],
            room_id=e1_create.room_id,
        )
        # 3. Alice is the creator
        e3_power1 = self.create_event(
            EventTypes.PowerLevels,
            "",
            sender=ALICE,
            content={"users": {}},
            auth_events=[e2_ma.event_id],
            room_id=e1_create.room_id,
        )
        # 4. Alice sets the room to public.
        e4_jr = self.create_event(
            EventTypes.JoinRules,
            "",
            sender=ALICE,
            content={"join_rule": JoinRules.PUBLIC},
            auth_events=[e2_ma.event_id, e3_power1.event_id],
            room_id=e1_create.room_id,
        )
        # 5. Bob joins the room.
        e5_mb = self.create_event(
            EventTypes.Member,
            BOB,
            sender=BOB,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[e3_power1.event_id, e4_jr.event_id],
            room_id=e1_create.room_id,
        )
        # 6. Charlie joins the room.
        e6_mc = self.create_event(
            EventTypes.Member,
            CHARLIE,
            sender=CHARLIE,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[e3_power1.event_id, e4_jr.event_id],
            room_id=e1_create.room_id,
        )
        # 7. Alice promotes Bob.
        e7_power2 = self.create_event(
            EventTypes.PowerLevels,
            "",
            sender=ALICE,
            content={"users": {BOB: 50}},
            auth_events=[e2_ma.event_id, e3_power1.event_id],
            room_id=e1_create.room_id,
        )
        # 8. Bob promotes Charlie.
        e8_power3 = self.create_event(
            EventTypes.PowerLevels,
            "",
            sender=BOB,
            content={"users": {BOB: 50, CHARLIE: 50}},
            auth_events=[e5_mb.event_id, e7_power2.event_id],
            room_id=e1_create.room_id,
        )
        # 9. Eve joins the room.
        e9_me1 = self.create_event(
            EventTypes.Member,
            EVELYN,
            sender=EVELYN,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[e8_power3.event_id, e4_jr.event_id],
            room_id=e1_create.room_id,
        )
        # 10. Eve changes her name, /!\\ but cites old power levels /!\
        e10_me2 = self.create_event(
            EventTypes.Member,
            EVELYN,
            sender=EVELYN,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[
                e3_power1.event_id,
                e4_jr.event_id,
                e9_me1.event_id,
            ],
            room_id=e1_create.room_id,
        )
        # 11. Zara joins the room, citing the most recent power levels.
        e11_mz = self.create_event(
            EventTypes.Member,
            ZARA,
            sender=ZARA,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[e8_power3.event_id, e4_jr.event_id],
            room_id=e1_create.room_id,
        )

        # Event 10 above is DODGY: it directly cites old auth events, but indirectly
        # cites new ones. If the state after event 10 contains old power level and old
        # join events, we are vulnerable to a reset.

        dodgy_state_after_eve_rename: StateMap[str] = {
            (EventTypes.Create, ""): e1_create.event_id,
            (EventTypes.Member, ALICE): e2_ma.event_id,
            (EventTypes.Member, BOB): e5_mb.event_id,
            (EventTypes.Member, CHARLIE): e6_mc.event_id,
            (EventTypes.Member, EVELYN): e10_me2.event_id,
            (EventTypes.PowerLevels, ""): e3_power1.event_id,  # old and /!\\ DODGY /!\
            (EventTypes.JoinRules, ""): e4_jr.event_id,
        }

        sensible_state_after_zara_joins: StateMap[str] = {
            (EventTypes.Create, ""): e1_create.event_id,
            (EventTypes.Member, ALICE): e2_ma.event_id,
            (EventTypes.Member, BOB): e5_mb.event_id,
            (EventTypes.Member, CHARLIE): e6_mc.event_id,
            (EventTypes.Member, ZARA): e11_mz.event_id,
            (EventTypes.PowerLevels, ""): e8_power3.event_id,
            (EventTypes.JoinRules, ""): e4_jr.event_id,
        }

        expected: StateMap[str] = {
            (EventTypes.Create, ""): e1_create.event_id,
            (EventTypes.Member, ALICE): e2_ma.event_id,
            (EventTypes.Member, BOB): e5_mb.event_id,
            (EventTypes.Member, CHARLIE): e6_mc.event_id,
            # Expect ME2 replayed first: it's in the POWER 1 epoch
            # Then ME1, in the POWER 3 epoch
            (EventTypes.Member, EVELYN): e9_me1.event_id,
            (EventTypes.Member, ZARA): e11_mz.event_id,
            (EventTypes.PowerLevels, ""): e8_power3.event_id,
            (EventTypes.JoinRules, ""): e4_jr.event_id,
        }

        self.get_resolution_and_verify_expected(
            [dodgy_state_after_eve_rename, sensible_state_after_zara_joins],
            [
                e1_create,
                e2_ma,
                e3_power1,
                e4_jr,
                e5_mb,
                e6_mc,
                e7_power2,
                e8_power3,
                e9_me1,
                e10_me2,
                e11_mz,
            ],
            expected,
        )

    def test_state_reset_start_empty_set(self) -> None:
        # The join rules reset to missing, when:
        # - join rules were in conflict
        # - the membership of those join rules' senders were not in conflict
        # - those memberships are all leaves.

        # 1. Alice creates a room.
        e1_create = self.create_event(
            EventTypes.Create,
            "",
            sender=ALICE,
            content={"creator": ALICE},
            auth_events=[],
        )
        # 2. Alice joins it.
        e2_ma1 = self.create_event(
            EventTypes.Member,
            ALICE,
            sender=ALICE,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[],
            room_id=e1_create.room_id,
        )
        # 3. Alice makes Bob an admin.
        e3_power = self.create_event(
            EventTypes.PowerLevels,
            "",
            sender=ALICE,
            content={"users": {BOB: 100}},
            auth_events=[e2_ma1.event_id],
            room_id=e1_create.room_id,
        )
        # 4. Alice sets the room to public.
        e4_jr1 = self.create_event(
            EventTypes.JoinRules,
            "",
            sender=ALICE,
            content={"join_rule": JoinRules.PUBLIC},
            auth_events=[e2_ma1.event_id, e3_power.event_id],
            room_id=e1_create.room_id,
        )
        # 5. Bob joins.
        e5_mb = self.create_event(
            EventTypes.Member,
            BOB,
            sender=BOB,
            content=MEMBERSHIP_CONTENT_JOIN,
            auth_events=[e3_power.event_id, e4_jr1.event_id],
            room_id=e1_create.room_id,
        )
        # 6. Alice sets join rules to invite.
        e6_jr2 = self.create_event(
            EventTypes.JoinRules,
            "",
            sender=ALICE,
            content={"join_rule": JoinRules.INVITE},
            auth_events=[e2_ma1.event_id, e3_power.event_id],
            room_id=e1_create.room_id,
        )
        # 7. Alice then leaves.
        e7_ma2 = self.create_event(
            EventTypes.Member,
            ALICE,
            sender=ALICE,
            content=MEMBERSHIP_CONTENT_LEAVE,
            auth_events=[e3_power.event_id, e2_ma1.event_id],
            room_id=e1_create.room_id,
        )

        correct_state: StateMap[str] = {
            (EventTypes.Create, ""): e1_create.event_id,
            (EventTypes.Member, ALICE): e7_ma2.event_id,
            (EventTypes.Member, BOB): e5_mb.event_id,
            (EventTypes.PowerLevels, ""): e3_power.event_id,
            (EventTypes.JoinRules, ""): e6_jr2.event_id,
        }

        # Imagine that another server gives us incorrect state on a fork
        # (via e.g. backfill). It cites the old join rules.
        incorrect_state: StateMap[str] = {
            (EventTypes.Create, ""): e1_create.event_id,
            (EventTypes.Member, ALICE): e7_ma2.event_id,
            (EventTypes.Member, BOB): e5_mb.event_id,
            (EventTypes.PowerLevels, ""): e3_power.event_id,
            (EventTypes.JoinRules, ""): e4_jr1.event_id,
        }

        # Resolving those two should give us the new join rules.
        expected: StateMap[str] = {
            (EventTypes.Create, ""): e1_create.event_id,
            (EventTypes.Member, ALICE): e7_ma2.event_id,
            (EventTypes.Member, BOB): e5_mb.event_id,
            (EventTypes.PowerLevels, ""): e3_power.event_id,
            (EventTypes.JoinRules, ""): e6_jr2.event_id,
        }

        self.get_resolution_and_verify_expected(
            [correct_state, incorrect_state],
            [e1_create, e2_ma1, e3_power, e4_jr1, e5_mb, e6_jr2, e7_ma2],
            expected,
        )

    async def _get_auth_difference_and_conflicted_subgraph(
        self,
        room_id: str,
        state_maps: Sequence[StateMap[str]],
        event_map: dict[str, EventBase] | None,
        state_res_store: StateResolutionStoreInterface,
    ) -> set[str]:
        _, conflicted_state = _seperate(state_maps)
        conflicted_set: set[str] | None = set(
            itertools.chain.from_iterable(conflicted_state.values())
        )
        if event_map is None:
            event_map = {}
        return await _get_auth_chain_difference(
            room_id,
            state_maps,
            event_map,
            state_res_store,
            conflicted_set,
        )

    def get_resolution_and_verify_expected(
        self,
        state_maps: Sequence[StateMap[str]],
        events: list[EventBase],
        expected: StateMap[str],
    ) -> None:
        room_id = events[0].room_id
        # First we try everything in-memory to check that the test case works.
        event_map = {ev.event_id: ev for ev in events}
        for ev in events:
            print(f"{ev.event_id} => {ev.type} {ev.state_key} => {ev.content}")
        resolution = self.get_success(
            resolve_events_with_store(
                FakeClock(),
                room_id,
                events[0].room_version,
                state_maps,
                event_map=event_map,
                state_res_store=TestStateResolutionStore(event_map),
            )
        )
        self.assertEqual(resolution, expected)

        got_auth_diff = self.get_success(
            self._get_auth_difference_and_conflicted_subgraph(
                room_id,
                state_maps,
                event_map,
                TestStateResolutionStore(event_map),
            )
        )
        # we should never see the create event in the auth diff. If we do, this implies the
        # conflicted subgraph is wrong and is returning too many old events.
        assert events[0].event_id not in got_auth_diff

        # now let's make the room exist on the DB, some queries rely on there being a row in
        # the rooms table when persisting
        self.get_success(
            self.store.store_room(
                room_id,
                events[0].sender,
                True,
                events[0].room_version,
            )
        )

        def resolve_and_check() -> None:
            event_map = {ev.event_id: ev for ev in events}
            store = StateResolutionStore(
                self._persistence.main_store,
                self._state_deletion,
            )
            resolution = self.get_success(
                resolve_events_with_store(
                    FakeClock(),
                    room_id,
                    RoomVersions.HydraV11,
                    state_maps,
                    event_map=event_map,
                    state_res_store=store,
                )
            )
            self.assertEqual(resolution, expected)
            got_auth_diff2 = self.get_success(
                self._get_auth_difference_and_conflicted_subgraph(
                    room_id,
                    state_maps,
                    event_map,
                    store,
                )
            )
            # no matter how many events are persisted, the overall diff should always be the same.
            self.assertEquals(got_auth_diff, got_auth_diff2)

        # now we will drip feed in `events` one-by-one, persisting them then resolving with the
        # rest. This ensures we correctly handle mixed persisted/unpersisted events. We will finish
        # with doing the test with all persisted events.
        while len(events) > 0:
            event_to_persist = events.pop(0)
            self.persist_event(event_to_persist)
            # now retest
            resolve_and_check()

    def persist_event(
        self, event: EventBase, state: StateMap[str] | None = None
    ) -> None:
        """Persist the event, with optional state"""
        context = self.get_success(
            self.state.compute_event_context(
                event,
                state_ids_before_event=state,
                partial_state=None if state is None else False,
            )
        )
        self.get_success(self._persistence.persist_event(event, context))

    def create_event(
        self,
        event_type: str,
        state_key: str | None,
        sender: str,
        content: dict,
        auth_events: list[str],
        prev_events: list[str] | None = None,
        room_id: str | None = None,
    ) -> EventBase:
        """Short-hand for event_from_pdu_json for fields we typically care about.
        Tests can override by just calling event_from_pdu_json directly."""
        if prev_events is None:
            prev_events = []

        pdu = {
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "sender": sender,
            "depth": 5,
            "prev_events": prev_events,
            "auth_events": auth_events,
            "origin_server_ts": monotonic_timestamp(),
        }
        if event_type != EventTypes.Create:
            if room_id is None:
                raise Exception("must specify a room_id to create_event")
            pdu["room_id"] = room_id
        return event_from_pdu_json(
            pdu,
            RoomVersions.HydraV11,
        )
