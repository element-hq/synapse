#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import logging
import sys
import traceback
from collections import deque
from math import floor
from typing import Any, Optional

logger = logging.getLogger(__name__)


class RemoteHandler(logging.Handler):
    """
    A logging handler that writes logs to a TCP target using asyncio.

    Args:
        host: The host of the logging target.
        port: The logging target's port.
        maximum_buffer: The maximum buffer size.
    """

    def __init__(
        self,
        host: str,
        port: int,
        maximum_buffer: int = 1000,
        level: int = logging.NOTSET,
        _reactor: Any = None,
    ):
        super().__init__(level=level)
        self.host = host
        self.port = port
        self.maximum_buffer = maximum_buffer

        self._buffer: deque[logging.LogRecord] = deque()
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connecting = False
        self._stopping = False

        # Try to connect immediately
        self._schedule_connect()

    def _schedule_connect(self) -> None:
        """Schedule a connection attempt on the event loop."""
        if self._connecting or self._stopping:
            return

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._connect())
            else:
                # Event loop not running yet; we'll try again on emit()
                pass
        except RuntimeError:
            pass  # No event loop

    async def _connect(self) -> None:
        """Connect to the remote logging target."""
        if self._connecting or self._stopping:
            return

        self._connecting = True
        try:
            _, writer = await asyncio.open_connection(self.host, self.port)
            self._writer = writer
            self._flush_buffer()
        except Exception:
            traceback.print_exc(file=sys.__stderr__)
            self._writer = None
            # Retry after a delay
            if not self._stopping:
                try:
                    loop = asyncio.get_event_loop()
                    loop.call_later(5, self._schedule_connect)
                except RuntimeError:
                    pass
        finally:
            self._connecting = False

    def _flush_buffer(self) -> None:
        """Write buffered log records to the transport."""
        if self._writer is None:
            return

        while self._buffer:
            try:
                record = self._buffer.popleft()
                msg = self.format(record)
                self._writer.write(msg.encode("utf-8"))
                self._writer.write(b"\n")
            except Exception:
                traceback.print_exc(file=sys.__stderr__)
                break

    def close(self) -> None:
        self._stopping = True
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        super().close()

    def _handle_pressure(self) -> None:
        """
        Handle backpressure by shedding records.

        The buffer will, in this order, until the buffer is below the maximum:
            - Shed DEBUG records.
            - Shed INFO records.
            - Shed the middle 50% of the records.
        """
        if len(self._buffer) <= self.maximum_buffer:
            return

        # Strip out DEBUGs
        self._buffer = deque(
            filter(lambda record: record.levelno > logging.DEBUG, self._buffer)
        )

        if len(self._buffer) <= self.maximum_buffer:
            return

        # Strip out INFOs
        self._buffer = deque(
            filter(lambda record: record.levelno > logging.INFO, self._buffer)
        )

        if len(self._buffer) <= self.maximum_buffer:
            return

        # Cut the middle entries out
        buffer_split = floor(self.maximum_buffer / 2)

        old_buffer = self._buffer
        self._buffer = deque()

        for _ in range(buffer_split):
            self._buffer.append(old_buffer.popleft())

        end_buffer = []
        for _ in range(buffer_split):
            end_buffer.append(old_buffer.pop())

        self._buffer.extend(reversed(end_buffer))

    def emit(self, record: logging.LogRecord) -> None:
        self._buffer.append(record)

        # Handle backpressure, if it exists.
        try:
            self._handle_pressure()
        except Exception:
            # If handling backpressure fails, clear the buffer and log the
            # exception.
            self._buffer.clear()
            logger.warning("Failed clearing backpressure")

        # Try to write immediately if connected
        if self._writer is not None:
            self._flush_buffer()
        else:
            # Try to connect
            self._schedule_connect()
