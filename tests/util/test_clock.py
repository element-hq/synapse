#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

import weakref

from synapse.synapse_rust import clock as rust_clock
from synapse.util.duration import Duration

from tests.unittest import HomeserverTestCase, TestCase


class ClockTestCase(HomeserverTestCase):
    def test_looping_calls_are_gced(self) -> None:
        """Test that looping calls are garbage collected after being stopped.

        The `Clock` tracks looping calls so to allow stopping of all looping
        calls via the clock.
        """
        clock = self.hs.get_clock()

        # Create a new looping call, and take a weakref to it.
        call = clock.looping_call(lambda: None, Duration(seconds=1))

        weak_call = weakref.ref(call)

        # Stop the looping call. It should get garbage collected after this.
        call.stop()

        # Delete our strong reference to the call (otherwise it won't get garbage collected).
        del call

        # Check that the call has been garbage collected.
        self.assertIsNone(weak_call())

    def test_looping_calls_stopped_on_clock_shutdown(self) -> None:
        """Test that looping calls are stopped when the clock is shut down."""
        clock = self.hs.get_clock()

        was_called = False

        def on_call() -> None:
            nonlocal was_called
            was_called = True

        # Create a new looping call.
        call = clock.looping_call(on_call, Duration(seconds=1))
        weak_call = weakref.ref(call)
        del call  # Remove our strong reference to the call.

        # The call should still exist.
        self.assertIsNotNone(weak_call())

        # Advance the clock to trigger the call.
        self.reactor.advance(2)
        self.assertTrue(was_called)

        # Shut down the clock, which should stop the looping call.
        clock.shutdown()

        # The call should have been garbage collected.
        self.assertIsNone(weak_call())

        # Advance the clock again; the call should not be called again.
        was_called = False
        self.reactor.advance(2)
        self.assertFalse(was_called)


class RustClockTestCase(HomeserverTestCase):
    """Tests for `synapse.synapse_rust.clock`, the Rust side's view of the time.

    Rust can't reach the (virtual) reactor clock the tests run against, so the
    test reactor pushes the time over to it; see
    `tests.server.ThreadedMemoryReactorClock.seconds`.
    """

    def test_matches_the_synapse_clock(self) -> None:
        self.assertEqual(rust_clock.time_msec(), self.hs.get_clock().time_msec())

    def test_follows_the_reactor(self) -> None:
        before = rust_clock.time_msec()

        self.reactor.advance(37)

        self.assertEqual(rust_clock.time_msec(), before + 37 * 1000)
        self.assertEqual(rust_clock.time_msec(), self.hs.get_clock().time_msec())

    def test_is_up_to_date_inside_a_looping_call(self) -> None:
        """Rust must see the new time from callbacks fired *during* an advance.

        This is why we hook `seconds()` rather than `advance()`: expiring things
        by age from a looping call is the main reason the Rust clock exists.
        """
        seen: list[int] = []
        self.hs.get_clock().looping_call(
            lambda: seen.append(rust_clock.time_msec()), Duration(seconds=1)
        )

        expected = self.hs.get_clock().time_msec() + 1000
        self.reactor.advance(1)

        self.assertEqual(seen, [expected])


class RealRustClockTestCase(TestCase):
    """The virtual time must not leak out of the tests that pin it."""

    def test_clock_is_real_without_a_virtual_reactor(self) -> None:
        # This test never builds a test reactor, so the Rust clock should be
        # reporting real time — as it will be for anything running in a real
        # homeserver. If a previous test leaked its pinned time we'd see that
        # instead, because the whole suite shares one process.
        self.assertGreater(rust_clock.time_msec(), 1_600_000_000_000)
