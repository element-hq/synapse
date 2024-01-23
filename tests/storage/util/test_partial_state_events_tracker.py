#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from typing import Collection, Dict
from unittest import mock

from twisted.internet.defer import CancelledError, ensureDeferred

from synapse.storage.util.partial_state_events_tracker import (
    PartialCurrentStateTracker,
    PartialStateEventsTracker,
)

from tests.unittest import TestCase


class PartialStateEventsTrackerTestCase(TestCase):
    def setUp(self) -> None:
        # the results to be returned by the mocked get_partial_state_events
        self._events_dict: Dict[str, bool] = {}

        async def get_partial_state_events(events: Collection[str]) -> Dict[str, bool]:
            return {e: self._events_dict[e] for e in events}

        self.mock_store = mock.Mock(spec_set=["get_partial_state_events"])
        self.mock_store.get_partial_state_events.side_effect = get_partial_state_events

        self.tracker = PartialStateEventsTracker(self.mock_store)

    def test_does_not_block_for_full_state_events(self) -> None:
        self._events_dict = {"event1": False, "event2": False}

        self.successResultOf(
            ensureDeferred(self.tracker.await_full_state(["event1", "event2"]))
        )

        self.mock_store.get_partial_state_events.assert_called_once_with(
            ["event1", "event2"]
        )

    def test_blocks_for_partial_state_events(self) -> None:
        self._events_dict = {"event1": True, "event2": False}

        d = ensureDeferred(self.tracker.await_full_state(["event1", "event2"]))

        # there should be no result yet
        self.assertNoResult(d)

        # notifying that the event has been de-partial-stated should unblock
        self.tracker.notify_un_partial_stated("event1")
        self.successResultOf(d)

    def test_un_partial_state_race(self) -> None:
        # if the event is un-partial-stated between the initial check and the
        # registration of the listener, it should not block.
        self._events_dict = {"event1": True, "event2": False}

        async def get_partial_state_events(events: Collection[str]) -> Dict[str, bool]:
            res = {e: self._events_dict[e] for e in events}
            # change the result for next time
            self._events_dict = {"event1": False, "event2": False}
            return res

        self.mock_store.get_partial_state_events.side_effect = get_partial_state_events

        self.successResultOf(
            ensureDeferred(self.tracker.await_full_state(["event1", "event2"]))
        )

    def test_un_partial_state_during_get_partial_state_events(self) -> None:
        # we should correctly handle a call to notify_un_partial_stated during the
        # second call to get_partial_state_events.

        self._events_dict = {"event1": True, "event2": False}

        async def get_partial_state_events1(events: Collection[str]) -> Dict[str, bool]:
            self.mock_store.get_partial_state_events.side_effect = (
                get_partial_state_events2
            )
            return {e: self._events_dict[e] for e in events}

        async def get_partial_state_events2(events: Collection[str]) -> Dict[str, bool]:
            self.tracker.notify_un_partial_stated("event1")
            self._events_dict["event1"] = False
            return {e: self._events_dict[e] for e in events}

        self.mock_store.get_partial_state_events.side_effect = get_partial_state_events1

        self.successResultOf(
            ensureDeferred(self.tracker.await_full_state(["event1", "event2"]))
        )

    def test_cancellation(self) -> None:
        self._events_dict = {"event1": True, "event2": False}

        d1 = ensureDeferred(self.tracker.await_full_state(["event1", "event2"]))
        self.assertNoResult(d1)

        d2 = ensureDeferred(self.tracker.await_full_state(["event1"]))
        self.assertNoResult(d2)

        d1.cancel()
        self.assertFailure(d1, CancelledError)

        # d2 should still be waiting!
        self.assertNoResult(d2)

        self.tracker.notify_un_partial_stated("event1")
        self.successResultOf(d2)


class PartialCurrentStateTrackerTestCase(TestCase):
    def setUp(self) -> None:
        self.mock_store = mock.Mock(spec_set=["is_partial_state_room"])
        self.mock_store.is_partial_state_room = mock.AsyncMock()

        self.tracker = PartialCurrentStateTracker(self.mock_store)

    def test_does_not_block_for_full_state_rooms(self) -> None:
        self.mock_store.is_partial_state_room.return_value = False

        self.successResultOf(ensureDeferred(self.tracker.await_full_state("room_id")))

    def test_blocks_for_partial_room_state(self) -> None:
        self.mock_store.is_partial_state_room.return_value = True

        d = ensureDeferred(self.tracker.await_full_state("room_id"))

        # there should be no result yet
        self.assertNoResult(d)

        # notifying that the room has been de-partial-stated should unblock
        self.tracker.notify_un_partial_stated("room_id")
        self.successResultOf(d)

    def test_un_partial_state_race(self) -> None:
        # We should correctly handle race between awaiting the state and us
        # un-partialling the state
        async def is_partial_state_room(room_id: str) -> bool:
            self.tracker.notify_un_partial_stated("room_id")
            return True

        self.mock_store.is_partial_state_room.side_effect = is_partial_state_room

        self.successResultOf(ensureDeferred(self.tracker.await_full_state("room_id")))

    def test_cancellation(self) -> None:
        self.mock_store.is_partial_state_room.return_value = True

        d1 = ensureDeferred(self.tracker.await_full_state("room_id"))
        self.assertNoResult(d1)

        d2 = ensureDeferred(self.tracker.await_full_state("room_id"))
        self.assertNoResult(d2)

        d1.cancel()
        self.assertFailure(d1, CancelledError)

        # d2 should still be waiting!
        self.assertNoResult(d2)

        self.tracker.notify_un_partial_stated("room_id")
        self.successResultOf(d2)
