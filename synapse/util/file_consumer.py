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

import asyncio
import queue
from typing import Any, BinaryIO, Optional, Union, cast

from synapse.logging.context import make_deferred_yieldable
from synapse.types import ISynapseReactor


class BackgroundFileConsumer:
    """A consumer that writes to a file like object. Supports both push
    and pull producers

    Args:
        file_obj: The file like object to write to. Closed when
            finished.
        reactor: the Twisted reactor to use
    """

    # For PushProducers pause if we have this many unwritten slices
    _PAUSE_ON_QUEUE_SIZE = 5
    # And resume once the size of the queue is less than this
    _RESUME_ON_QUEUE_SIZE = 2

    def __init__(self, file_obj: BinaryIO, reactor: ISynapseReactor) -> None:
        self._file_obj: BinaryIO = file_obj

        self._reactor: ISynapseReactor = reactor

        # Producer we're registered with
        self._producer: Optional[Union[Any, Any]] = None

        # True if PushProducer, false if PullProducer
        self.streaming = False

        # For PushProducers, indicates whether we've paused the producer and
        # need to call resumeProducing before we get more data.
        self._paused_producer = False

        # Queue of slices of bytes to be written. When producer calls
        # unregister a final None is sent.
        self._bytes_queue: queue.Queue[bytes | None] = queue.Queue()

        # asyncio.Future that is resolved when finished writing
        #
        # This is really asyncio.Future[None], but mypy doesn't seem to like that.
        self._finished_deferred: asyncio.Future[Any] | None = None

        # If the _writer thread throws an exception it gets stored here.
        self._write_exception: Exception | None = None

    def registerProducer(
        self, producer: Union[Any, Any], streaming: bool
    ) -> None:
        """Part of IConsumer interface

        Args:
            producer
            streaming: True if push based producer, False if pull
                based.
        """
        if self._producer:
            raise Exception("registerProducer called twice")

        self._producer = producer
        self.streaming = streaming
        self._loop = asyncio.get_event_loop()
        self._finished_deferred = self._loop.run_in_executor(None, self._writer)
        if not streaming:
            self._producer.resumeProducing()

    def unregisterProducer(self) -> None:
        """Part of IProducer interface"""
        self._producer = None
        assert self._finished_deferred is not None
        if not self._finished_deferred.done():
            self._bytes_queue.put_nowait(None)

    def write(self, write_bytes: bytes) -> None:
        """Part of IProducer interface"""
        if self._write_exception:
            raise self._write_exception

        assert self._finished_deferred is not None
        if self._finished_deferred.done():
            raise Exception("consumer has closed")

        self._bytes_queue.put_nowait(write_bytes)

        # If this is a PushProducer and the queue is getting behind
        # then we pause the producer.
        if self.streaming and self._bytes_queue.qsize() >= self._PAUSE_ON_QUEUE_SIZE:
            self._paused_producer = True
            assert self._producer is not None
            # cast safe because `streaming` means this is an Any
            cast(Any, self._producer).pauseProducing()

    def _writer(self) -> None:
        """This is run in a background thread to write to the file."""
        loop = self._loop
        try:
            while self._producer or not self._bytes_queue.empty():
                # If we've paused the producer check if we should resume the
                # producer.
                if self._producer and self._paused_producer:
                    if self._bytes_queue.qsize() <= self._RESUME_ON_QUEUE_SIZE:
                        loop.call_soon_threadsafe(self._resume_paused_producer)

                bytes = self._bytes_queue.get()

                # If we get a None (or empty list) then that's a signal used
                # to indicate we should check if we should stop.
                if bytes:
                    self._file_obj.write(bytes)

                # If its a pull producer then we need to explicitly ask for
                # more stuff.
                if not self.streaming and self._producer:
                    loop.call_soon_threadsafe(self._producer.resumeProducing)
        except Exception as e:
            self._write_exception = e
            raise
        finally:
            self._file_obj.close()

    def wait(self) -> "asyncio.Future[None]":
        """Returns a deferred that resolves when finished writing to file"""
        assert self._finished_deferred is not None
        return make_deferred_yieldable(self._finished_deferred)

    def _resume_paused_producer(self) -> None:
        """Gets called if we should resume producing after being paused"""
        if self._paused_producer and self._producer:
            self._paused_producer = False
            self._producer.resumeProducing()
