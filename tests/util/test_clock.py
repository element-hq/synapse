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

from synapse.util.duration import Duration

from tests.unittest import HomeserverTestCase


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
