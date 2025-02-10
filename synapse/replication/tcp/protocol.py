#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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
"""This module contains the implementation of both the client and server
protocols.

An explanation of this protocol is available in docs/tcp_replication.md
"""

import fcntl
import logging
import struct
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Collection, List, Optional

from prometheus_client import Counter
from zope.interface import Interface, implementer

from twisted.internet import task
from twisted.internet.tcp import Connection
from twisted.protocols.basic import LineOnlyReceiver
from twisted.python.failure import Failure

from synapse.logging.context import PreserveLoggingContext
from synapse.metrics import LaterGauge
from synapse.metrics.background_process_metrics import (
    BackgroundProcessLoggingContext,
    run_as_background_process,
)
from synapse.replication.tcp.commands import (
    VALID_CLIENT_COMMANDS,
    VALID_SERVER_COMMANDS,
    Command,
    ErrorCommand,
    NameCommand,
    PingCommand,
    ReplicateCommand,
    ServerCommand,
    parse_command_from_line,
)
from synapse.util import Clock
from synapse.util.stringutils import random_string

if TYPE_CHECKING:
    from synapse.replication.tcp.handler import ReplicationCommandHandler
    from synapse.server import HomeServer


connection_close_counter = Counter(
    "synapse_replication_tcp_protocol_close_reason", "", ["reason_type"]
)

tcp_inbound_commands_counter = Counter(
    "synapse_replication_tcp_protocol_inbound_commands",
    "Number of commands received from replication, by command and name of process connected to",
    ["command", "name"],
)

tcp_outbound_commands_counter = Counter(
    "synapse_replication_tcp_protocol_outbound_commands",
    "Number of commands sent to replication, by command and name of process connected to",
    ["command", "name"],
)

# A list of all connected protocols. This allows us to send metrics about the
# connections.
connected_connections: "List[BaseReplicationStreamProtocol]" = []


logger = logging.getLogger(__name__)


PING_TIME = 5000
PING_TIMEOUT_MULTIPLIER = 5
PING_TIMEOUT_MS = PING_TIME * PING_TIMEOUT_MULTIPLIER


class ConnectionStates:
    CONNECTING = "connecting"
    ESTABLISHED = "established"
    PAUSED = "paused"
    CLOSED = "closed"


class IReplicationConnection(Interface):
    """An interface for replication connections."""

    def send_command(cmd: Command) -> None:
        """Send the command down the connection"""


@implementer(IReplicationConnection)
class BaseReplicationStreamProtocol(LineOnlyReceiver):
    """Base replication protocol shared between client and server.

    Reads lines (ignoring blank ones) and parses them into command classes,
    asserting that they are valid for the given direction, i.e. server commands
    are only sent by the server.

    On receiving a new command it calls `on_<COMMAND_NAME>` with the parsed
    command before delegating to `ReplicationCommandHandler.on_<COMMAND_NAME>`.
    `ReplicationCommandHandler.on_<COMMAND_NAME>` can optionally return a coroutine;
    if so, that will get run as a background process.

    It also sends `PING` periodically, and correctly times out remote connections
    (if they send a `PING` command)
    """

    # The transport is going to be an ITCPTransport, but that doesn't have the
    # (un)registerProducer methods, those are only on the implementation.
    transport: Connection

    delimiter = b"\n"

    # Valid commands we expect to receive
    VALID_INBOUND_COMMANDS: Collection[str] = []

    # Valid commands we can send
    VALID_OUTBOUND_COMMANDS: Collection[str] = []

    max_line_buffer = 10000

    def __init__(self, clock: Clock, handler: "ReplicationCommandHandler"):
        self.clock = clock
        self.command_handler = handler

        self.last_received_command = self.clock.time_msec()
        self.last_sent_command = 0
        # When we requested the connection be closed
        self.time_we_closed: Optional[int] = None

        self.received_ping = False  # Have we received a ping from the other side

        self.state = ConnectionStates.CONNECTING

        self.name = "anon"  # The name sent by a client.
        self.conn_id = random_string(5)  # To dedupe in case of name clashes.

        # List of pending commands to send once we've established the connection
        self.pending_commands: List[Command] = []

        # The LoopingCall for sending pings.
        self._send_ping_loop: Optional[task.LoopingCall] = None

        # a logcontext which we use for processing incoming commands. We declare it as a
        # background process so that the CPU stats get reported to prometheus.
        with PreserveLoggingContext():
            # thanks to `PreserveLoggingContext()`, the new logcontext is guaranteed to
            # capture the sentinel context as its containing context and won't prevent
            # GC of / unintentionally reactivate what would be the current context.
            self._logging_context = BackgroundProcessLoggingContext(
                "replication-conn", self.conn_id
            )

    def connectionMade(self) -> None:
        logger.info("[%s] Connection established", self.id())

        self.state = ConnectionStates.ESTABLISHED

        connected_connections.append(self)  # Register connection for metrics

        assert self.transport is not None
        self.transport.registerProducer(self, True)  # For the *Producing callbacks

        self._send_pending_commands()

        # Starts sending pings
        self._send_ping_loop = self.clock.looping_call(self.send_ping, 5000)

        # Always send the initial PING so that the other side knows that they
        # can time us out.
        self.send_command(PingCommand(str(self.clock.time_msec())))

        self.command_handler.new_connection(self)

    def send_ping(self) -> None:
        """Periodically sends a ping and checks if we should close the connection
        due to the other side timing out.
        """
        now = self.clock.time_msec()

        if self.time_we_closed:
            if now - self.time_we_closed > PING_TIMEOUT_MS:
                logger.info(
                    "[%s] Failed to close connection gracefully, aborting", self.id()
                )
                assert self.transport is not None
                self.transport.abortConnection()
        else:
            if now - self.last_sent_command >= PING_TIME:
                self.send_command(PingCommand(str(now)))

            if (
                self.received_ping
                and now - self.last_received_command > PING_TIMEOUT_MS
            ):
                logger.info(
                    "[%s] Connection hasn't received command in %r ms. Closing.",
                    self.id(),
                    now - self.last_received_command,
                )
                self.send_error("ping timeout")

    def lineReceived(self, line: bytes) -> None:
        """Called when we've received a line"""
        with PreserveLoggingContext(self._logging_context):
            self._parse_and_dispatch_line(line)

    def _parse_and_dispatch_line(self, line: bytes) -> None:
        if line.strip() == b"":
            # Ignore blank lines
            return

        linestr = line.decode("utf-8")

        try:
            cmd = parse_command_from_line(linestr)
        except Exception as e:
            logger.exception("[%s] failed to parse line: %r", self.id(), linestr)
            self.send_error("failed to parse line: %r (%r):" % (e, linestr))
            return

        if cmd.NAME not in self.VALID_INBOUND_COMMANDS:
            logger.error("[%s] invalid command %s", self.id(), cmd.NAME)
            self.send_error("invalid command: %s", cmd.NAME)
            return

        self.last_received_command = self.clock.time_msec()

        tcp_inbound_commands_counter.labels(cmd.NAME, self.name).inc()

        self.handle_command(cmd)

    def handle_command(self, cmd: Command) -> None:
        """Handle a command we have received over the replication stream.

        First calls `self.on_<COMMAND>` if it exists, then calls
        `self.command_handler.on_<COMMAND>` if it exists (which can optionally
        return an Awaitable).

        This allows for protocol level handling of commands (e.g. PINGs), before
        delegating to the handler.

        Args:
            cmd: received command
        """
        handled = False

        # First call any command handlers on this instance. These are for TCP
        # specific handling.
        cmd_func = getattr(self, "on_%s" % (cmd.NAME,), None)
        if cmd_func:
            cmd_func(cmd)
            handled = True

        # Then call out to the handler.
        cmd_func = getattr(self.command_handler, "on_%s" % (cmd.NAME,), None)
        if cmd_func:
            res = cmd_func(self, cmd)

            # the handler might be a coroutine: fire it off as a background process
            # if so.

            if isawaitable(res):
                run_as_background_process(
                    "replication-" + cmd.get_logcontext_id(), lambda: res
                )

            handled = True

        if not handled:
            logger.warning("Unhandled command: %r", cmd)

    def close(self) -> None:
        logger.warning("[%s] Closing connection", self.id())
        self.time_we_closed = self.clock.time_msec()
        assert self.transport is not None
        self.transport.loseConnection()
        self.on_connection_closed()

    def send_error(self, error_string: str, *args: Any) -> None:
        """Send an error to remote and close the connection."""
        self.send_command(ErrorCommand(error_string % args))
        self.close()

    def send_command(self, cmd: Command, do_buffer: bool = True) -> None:
        """Send a command if connection has been established.

        Args:
            cmd
            do_buffer: Whether to buffer the message or always attempt
                to send the command. This is mostly used to send an error
                message if we're about to close the connection due our buffers
                becoming full.
        """
        if self.state == ConnectionStates.CLOSED:
            logger.debug("[%s] Not sending, connection closed", self.id())
            return

        if do_buffer and self.state != ConnectionStates.ESTABLISHED:
            self._queue_command(cmd)
            return

        tcp_outbound_commands_counter.labels(cmd.NAME, self.name).inc()

        string = "%s %s" % (cmd.NAME, cmd.to_line())
        if "\n" in string:
            raise Exception("Unexpected newline in command: %r", string)

        encoded_string = string.encode("utf-8")

        if len(encoded_string) > self.MAX_LENGTH:
            raise Exception(
                "Failed to send command %s as too long (%d > %d)"
                % (cmd.NAME, len(encoded_string), self.MAX_LENGTH)
            )

        self.sendLine(encoded_string)

        self.last_sent_command = self.clock.time_msec()

    def _queue_command(self, cmd: Command) -> None:
        """Queue the command until the connection is ready to write to again."""
        logger.debug("[%s] Queueing as conn %r, cmd: %r", self.id(), self.state, cmd)
        self.pending_commands.append(cmd)

        if len(self.pending_commands) > self.max_line_buffer:
            # The other side is failing to keep up and out buffers are becoming
            # full, so lets close the connection.
            # XXX: should we squawk more loudly?
            logger.error("[%s] Remote failed to keep up", self.id())
            self.send_command(ErrorCommand("Failed to keep up"), do_buffer=False)
            self.close()

    def _send_pending_commands(self) -> None:
        """Send any queued commandes"""
        pending = self.pending_commands
        self.pending_commands = []
        for cmd in pending:
            self.send_command(cmd)

    def on_PING(self, cmd: PingCommand) -> None:
        self.received_ping = True

    def on_ERROR(self, cmd: ErrorCommand) -> None:
        logger.error("[%s] Remote reported error: %r", self.id(), cmd.data)

    def pauseProducing(self) -> None:
        """This is called when both the kernel send buffer and the twisted
        tcp connection send buffers have become full.

        We don't actually have any control over those sizes, so we buffer some
        commands ourselves before knifing the connection due to the remote
        failing to keep up.
        """
        logger.info("[%s] Pause producing", self.id())
        self.state = ConnectionStates.PAUSED

    def resumeProducing(self) -> None:
        """The remote has caught up after we started buffering!"""
        logger.info("[%s] Resume producing", self.id())
        self.state = ConnectionStates.ESTABLISHED
        self._send_pending_commands()

    def stopProducing(self) -> None:
        """We're never going to send any more data (normally because either
        we or the remote has closed the connection)
        """
        logger.info("[%s] Stop producing", self.id())
        self.on_connection_closed()

    def connectionLost(self, reason: Failure) -> None:  # type: ignore[override]
        logger.info("[%s] Replication connection closed: %r", self.id(), reason)
        if isinstance(reason, Failure):
            assert reason.type is not None
            connection_close_counter.labels(reason.type.__name__).inc()
        else:
            connection_close_counter.labels(reason.__class__.__name__).inc()  # type: ignore[unreachable]

        try:
            # Remove us from list of connections to be monitored
            connected_connections.remove(self)
        except ValueError:
            pass

        # Stop the looping call sending pings.
        if self._send_ping_loop and self._send_ping_loop.running:
            self._send_ping_loop.stop()

        self.on_connection_closed()

    def on_connection_closed(self) -> None:
        logger.info("[%s] Connection was closed", self.id())

        self.state = ConnectionStates.CLOSED
        self.pending_commands = []

        self.command_handler.lost_connection(self)

        if self.transport:
            self.transport.unregisterProducer()

        # mark the logging context as finished by triggering `__exit__()`
        with PreserveLoggingContext():
            with self._logging_context:
                pass
            # the sentinel context is now active, which may not be correct.
            # PreserveLoggingContext() will restore the correct logging context.

    def __str__(self) -> str:
        addr = None
        if self.transport:
            addr = str(self.transport.getPeer())
        return "ReplicationConnection<name=%s,conn_id=%s,addr=%s>" % (
            self.name,
            self.conn_id,
            addr,
        )

    def id(self) -> str:
        return "%s-%s" % (self.name, self.conn_id)

    def lineLengthExceeded(self, line: str) -> None:
        """Called when we receive a line that is above the maximum line length"""
        self.send_error("Line length exceeded")


class ServerReplicationStreamProtocol(BaseReplicationStreamProtocol):
    VALID_INBOUND_COMMANDS = VALID_CLIENT_COMMANDS
    VALID_OUTBOUND_COMMANDS = VALID_SERVER_COMMANDS

    def __init__(
        self, server_name: str, clock: Clock, handler: "ReplicationCommandHandler"
    ):
        super().__init__(clock, handler)

        self.server_name = server_name

    def connectionMade(self) -> None:
        self.send_command(ServerCommand(self.server_name))
        super().connectionMade()

    def on_NAME(self, cmd: NameCommand) -> None:
        logger.info("[%s] Renamed to %r", self.id(), cmd.data)
        self.name = cmd.data


class ClientReplicationStreamProtocol(BaseReplicationStreamProtocol):
    VALID_INBOUND_COMMANDS = VALID_SERVER_COMMANDS
    VALID_OUTBOUND_COMMANDS = VALID_CLIENT_COMMANDS

    def __init__(
        self,
        hs: "HomeServer",
        client_name: str,
        server_name: str,
        clock: Clock,
        command_handler: "ReplicationCommandHandler",
    ):
        super().__init__(clock, command_handler)

        self.client_name = client_name
        self.server_name = server_name

    def connectionMade(self) -> None:
        self.send_command(NameCommand(self.client_name))
        super().connectionMade()

        # Once we've connected subscribe to the necessary streams
        self.replicate()

    def on_SERVER(self, cmd: ServerCommand) -> None:
        if cmd.data != self.server_name:
            logger.error("[%s] Connected to wrong remote: %r", self.id(), cmd.data)
            self.send_error("Wrong remote")

    def replicate(self) -> None:
        """Send the subscription request to the server"""
        logger.info("[%s] Subscribing to replication streams", self.id())

        self.send_command(ReplicateCommand())


# The following simply registers metrics for the replication connections

pending_commands = LaterGauge(
    "synapse_replication_tcp_protocol_pending_commands",
    "",
    ["name"],
    lambda: {(p.name,): len(p.pending_commands) for p in connected_connections},
)


def transport_buffer_size(protocol: BaseReplicationStreamProtocol) -> int:
    if protocol.transport:
        size = len(protocol.transport.dataBuffer) + protocol.transport._tempDataLen
        return size
    return 0


transport_send_buffer = LaterGauge(
    "synapse_replication_tcp_protocol_transport_send_buffer",
    "",
    ["name"],
    lambda: {(p.name,): transport_buffer_size(p) for p in connected_connections},
)


def transport_kernel_read_buffer_size(
    protocol: BaseReplicationStreamProtocol, read: bool = True
) -> int:
    SIOCINQ = 0x541B
    SIOCOUTQ = 0x5411

    if protocol.transport:
        fileno = protocol.transport.getHandle().fileno()
        if read:
            op = SIOCINQ
        else:
            op = SIOCOUTQ
        size = struct.unpack("I", fcntl.ioctl(fileno, op, b"\0\0\0\0"))[0]
        return size
    return 0


tcp_transport_kernel_send_buffer = LaterGauge(
    "synapse_replication_tcp_protocol_transport_kernel_send_buffer",
    "",
    ["name"],
    lambda: {
        (p.name,): transport_kernel_read_buffer_size(p, False)
        for p in connected_connections
    },
)


tcp_transport_kernel_read_buffer = LaterGauge(
    "synapse_replication_tcp_protocol_transport_kernel_read_buffer",
    "",
    ["name"],
    lambda: {
        (p.name,): transport_kernel_read_buffer_size(p, True)
        for p in connected_connections
    },
)
