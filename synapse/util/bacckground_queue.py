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
#

import collections
import logging
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Generic,
    Optional,
    TypeVar,
)

from synapse.util.async_helpers import DeferredEvent

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BackgroundQueue(Generic[T]):
    """A single-producer single-consumer async queue processing items in the
    background.

    This is optimised for the case where we receive many items, but processing
    each one takes a short amount of time. In this case we don't want to pay the
    overhead of a new background process each time. Instead, we spawn a
    background process that will wait for new items to arrive.

    If the background process has been idle for a while, it will exit, and a new
    background process will be spawned when new items arrive.

    Args:
        hs: The homeserver.
        name: The name of the background process.
        callback: The async callback to process each item.
        timeout_ms: The time in milliseconds to wait for new items before
            exiting the background process.
    """

    def __init__(
        self,
        hs: "HomeServer",
        name: str,
        callback: Callable[[T], Awaitable[None]],
        timeout_ms: int = 1000,
    ) -> None:
        self._hs = hs
        self._name = name
        self._callback = callback
        self._timeout_ms = timeout_ms

        # The queue of items to process.
        self._queue: collections.deque[T] = collections.deque()

        # Indicates if a background process is running, and if so whether there
        # is new data in the queue.
        self._event: Optional[DeferredEvent] = None

    def add(self, item: T) -> None:
        """Add an item into the queue."""

        self._queue.append(item)
        if self._event is None:
            self._hs.run_as_background_process(self._name, self._process_queue)
        else:
            self._event.set()

    async def _process_queue(self) -> None:
        """Process items in the queue until it is empty."""

        # Make sure we're the only background process.
        if self._event is not None:
            self._event.set()
            return

        self._event = DeferredEvent(self._hs.get_clock())

        try:
            while True:
                # Clear the event before checking the queue.
                self._event.clear()

                while self._queue:
                    item = self._queue.popleft()
                    try:
                        await self._callback(item)
                    except Exception:
                        logger.exception("Error processing background queue item")

                # Wait for new data to arrive, timing out after a while to avoid
                # keeping the background process alive forever.
                #
                # New data may have arrived and been processed while we were
                # pulling from the queue, so this may return that there is new
                # data immediately even though there isn't. That's fine, we'll
                # just loop round, clear the event, recheck the queue, and then
                # wait here again.
                new_data = await self._event.wait(
                    timeout_seconds=self._timeout_ms / 1000
                )
                if not new_data:
                    # Timed out waiting for new data, so exit the loop
                    break
        finally:
            self._event = None

    def __len__(self) -> int:
        return len(self._queue)
