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

import logging
import sys
import traceback
from collections import deque
from ipaddress import IPv4Address, IPv6Address, ip_address
from math import floor
from typing import Callable

import attr
from zope.interface import implementer

from twisted.application.internet import ClientService
from twisted.internet.defer import CancelledError, Deferred
from twisted.internet.endpoints import (
    HostnameEndpoint,
    TCP4ClientEndpoint,
    TCP6ClientEndpoint,
)
from twisted.internet.interfaces import (
    IPushProducer,
    IReactorTime,
    IStreamClientEndpoint,
)
from twisted.internet.protocol import Factory, Protocol
from twisted.internet.tcp import Connection
from twisted.python.failure import Failure

logger = logging.getLogger(__name__)


@attr.s(slots=True, auto_attribs=True)
@implementer(IPushProducer)
class LogProducer:
    """
    An IPushProducer that writes logs from its buffer to its transport when it
    is resumed.

    Args:
        buffer: Log buffer to read logs from.
        transport: Transport to write to.
        format: A callable to format the log record to a string.
    """

    # This is essentially ITCPTransport, but that is missing certain fields
    # (connected and registerProducer) which are part of the implementation.
    transport: Connection
    _format: Callable[[logging.LogRecord], str]
    _buffer: deque[logging.LogRecord]
    _paused: bool = attr.ib(default=False, init=False)

    def pauseProducing(self) -> None:
        self._paused = True

    def stopProducing(self) -> None:
        self._paused = True
        self._buffer = deque()

    def resumeProducing(self) -> None:
        # If we're already producing, nothing to do.
        self._paused = False

        # Loop until paused.
        while self._paused is False and (self._buffer and self.transport.connected):
            try:
                # Request the next record and format it.
                record = self._buffer.popleft()
                msg = self._format(record)

                # Send it as a new line over the transport.
                self.transport.write(msg.encode("utf8"))
                self.transport.write(b"\n")
            except Exception:
                # Something has gone wrong writing to the transport -- log it
                # and break out of the while.
                traceback.print_exc(file=sys.__stderr__)
                break


class RemoteHandler(logging.Handler):
    """
    An logging handler that writes logs to a TCP target.

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
        _reactor: IReactorTime | None = None,
    ):
        super().__init__(level=level)
        self.host = host
        self.port = port
        self.maximum_buffer = maximum_buffer

        self._buffer: deque[logging.LogRecord] = deque()
        self._connection_waiter: Deferred | None = None
        self._producer: LogProducer | None = None

        # Connect without DNS lookups if it's a direct IP.
        if _reactor is None:
            from twisted.internet import reactor

            _reactor = reactor  # type: ignore[assignment]

        try:
            ip = ip_address(self.host)
            if isinstance(ip, IPv4Address):
                endpoint: IStreamClientEndpoint = TCP4ClientEndpoint(
                    _reactor, self.host, self.port
                )
            elif isinstance(ip, IPv6Address):
                endpoint = TCP6ClientEndpoint(_reactor, self.host, self.port)
            else:
                raise ValueError("Unknown IP address provided: %s" % (self.host,))
        except ValueError:
            endpoint = HostnameEndpoint(_reactor, self.host, self.port)

        factory = Factory.forProtocol(Protocol)
        self._service = ClientService(endpoint, factory, clock=_reactor)
        self._service.startService()
        self._stopping = False
        self._connect()

    def close(self) -> None:
        self._stopping = True
        self._service.stopService()

    def _connect(self) -> None:
        """
        Triggers an attempt to connect then write to the remote if not already writing.
        """
        # Do not attempt to open multiple connections.
        if self._connection_waiter:
            return

        def fail(failure: Failure) -> None:
            # If the Deferred was cancelled (e.g. during shutdown) do not try to
            # reconnect (this will cause an infinite loop of errors).
            if failure.check(CancelledError) and self._stopping:
                return

            # For a different error, print the traceback and re-connect.
            failure.printTraceback(file=sys.__stderr__)
            self._connection_waiter = None
            self._connect()

        def writer(result: Protocol) -> None:
            # Force recognising transport as a Connection and not the more
            # generic ITransport.
            transport: Connection = result.transport  # type: ignore

            # We have a connection. If we already have a producer, and its
            # transport is the same, just trigger a resumeProducing.
            if self._producer and transport is self._producer.transport:
                self._producer.resumeProducing()
                self._connection_waiter = None
                return

            # If the producer is still producing, stop it.
            if self._producer:
                self._producer.stopProducing()

            # Make a new producer and start it.
            self._producer = LogProducer(
                buffer=self._buffer,
                transport=transport,
                format=self.format,
            )
            transport.registerProducer(self._producer, True)
            self._producer.resumeProducing()
            self._connection_waiter = None

        deferred: Deferred = self._service.whenConnected(failAfterFailures=1)
        deferred.addCallbacks(writer, fail)
        self._connection_waiter = deferred

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

        # Try and write immediately.
        self._connect()
