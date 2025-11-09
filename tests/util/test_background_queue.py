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


from unittest.mock import Mock

from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor

from synapse.logging.context import PreserveLoggingContext, make_deferred_yieldable
from synapse.server import HomeServer
from synapse.util.background_queue import BackgroundQueue
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, logcontext_clean


class BackgroundQueueTests(HomeserverTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self._process_item_mock = Mock(spec_set=[])

        self.queue = BackgroundQueue[int](
            hs=homeserver,
            name="test_queue",
            callback=self._process_item_mock,
            timeout_ms=1000,
        )

    @logcontext_clean
    def test_simple_call(self) -> None:
        """Test that items added to the queue are processed."""
        # Register a deferred to be the return value of the callback.
        callback_result_deferred: Deferred[None] = Deferred()
        self._process_item_mock.side_effect = lambda _: make_deferred_yieldable(
            callback_result_deferred
        )

        # Adding an item should cause the callback to be invoked.
        self.queue.add(1)

        self._process_item_mock.assert_called_once_with(1)
        self._process_item_mock.reset_mock()

        # Adding another item should not cause the callback to be invoked again
        # until the previous one has completed.
        self.queue.add(2)
        self._process_item_mock.assert_not_called()

        # Once the first callback completes, the second item should be
        # processed.
        with PreserveLoggingContext():
            callback_result_deferred.callback(None)
        self._process_item_mock.assert_called_once_with(2)

    @logcontext_clean
    def test_timeout(self) -> None:
        """Test that the background process wakes up if its idle, and that it
        times out after being idle."""

        # Register a deferred to be the return value of the callback.
        callback_result_deferred: Deferred[None] = Deferred()
        self._process_item_mock.side_effect = lambda _: make_deferred_yieldable(
            callback_result_deferred
        )

        # Adding an item should cause the callback to be invoked.
        self.queue.add(1)

        self._process_item_mock.assert_called_once_with(1)
        self._process_item_mock.reset_mock()

        # Let the callback complete.
        with PreserveLoggingContext():
            callback_result_deferred.callback(None)

        # Advance the clock by less than the timeout, and add another item.
        self.reactor.advance(0.5)
        self.assertIsNotNone(self.queue._wakeup_event)
        self.queue.add(2)

        # The callback should be invoked again.
        callback_result_deferred = Deferred()
        self._process_item_mock.side_effect = lambda _: make_deferred_yieldable(
            callback_result_deferred
        )
        self._process_item_mock.assert_called_once_with(2)
        self._process_item_mock.reset_mock()

        # Let the callback complete.
        with PreserveLoggingContext():
            callback_result_deferred.callback(None)

        # Advance the clock by more than the timeout.
        self.reactor.advance(1.5)

        # The background process should have exited, we check this by checking
        # the internal wakeup event has been removed.
        self.assertIsNone(self.queue._wakeup_event)

        # Add another item. This should cause a new background process to be
        # started.
        self.queue.add(3)

        self._process_item_mock.assert_called_once_with(3)
