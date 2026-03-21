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

"""asyncio-native replication protocol using asyncio streams.

Phase 6 of the Twisted → asyncio migration. Provides NativeReplicationProtocol
as a replacement for BaseReplicationStreamProtocol, using
asyncio.StreamReader/StreamWriter instead of Twisted's LineOnlyReceiver.

This module is unused until later phases switch the replication layer to use it.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from synapse.replication.tcp.commands import (
    Command,
    PingCommand,
    parse_command_from_line,
)

logger = logging.getLogger(__name__)

# Ping interval in seconds
PING_INTERVAL = 5.0

# If no command received in this many seconds, close the connection
PING_TIMEOUT = 25.0

# Maximum number of buffered commands before we close the connection
MAX_PENDING_COMMANDS = 10000

# Maximum line length (matching Twisted's LineOnlyReceiver default)
MAX_LINE_LENGTH = 16384


class ConnectionState:
    CONNECTING = "CONNECTING"
    ESTABLISHED = "ESTABLISHED"
    CLOSED = "CLOSED"


class NativeReplicationProtocol:
    """asyncio-native equivalent of BaseReplicationStreamProtocol.

    Implements a line-based replication protocol using asyncio.StreamReader
    and asyncio.StreamWriter instead of Twisted's LineOnlyReceiver.

    The protocol is line-based: each line is a command in the format
    "<COMMAND_NAME> <DATA>\\n". Commands are parsed by
    parse_command_from_line() from the existing commands module.

    Subclass and override on_connection_made(), on_connection_lost(),
    and command handlers (on_<COMMAND_NAME> methods) to implement
    server or client behavior.
    """

    def __init__(
        self,
        server_name: str,
        command_handler: Any = None,
        valid_inbound_commands: frozenset[str] | None = None,
        valid_outbound_commands: frozenset[str] | None = None,
    ) -> None:
        self._server_name = server_name
        self._command_handler = command_handler
        self._valid_inbound = valid_inbound_commands or frozenset()
        self._valid_outbound = valid_outbound_commands or frozenset()

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._state = ConnectionState.CONNECTING
        self._pending_commands: list[Command] = []

        self._last_sent_command = 0.0
        self._last_received_command = 0.0
        self._received_ping = False

        self._ping_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._time_we_closed: float | None = None

        self.conn_id: str = ""

    async def start(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Initialize the protocol with established stream pair.

        Call this after accepting a connection (server) or connecting (client).
        """
        self._reader = reader
        self._writer = writer
        self._state = ConnectionState.ESTABLISHED
        self._last_received_command = time.time()
        self._last_sent_command = time.time()

        peername = writer.get_extra_info("peername")
        self.conn_id = f"{peername}" if peername else "unknown"

        logger.info("Replication connection established: %s", self.conn_id)

        # Send initial ping to enable timeout mechanism
        await self.send_command(PingCommand(str(int(time.time() * 1000))))

        # Send any pending commands
        await self._send_pending_commands()

        # Notify subclass
        await self.on_connection_made()

        # Start background tasks
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        """Read lines from the stream and dispatch commands."""
        assert self._reader is not None
        try:
            while self._state != ConnectionState.CLOSED:
                try:
                    line = await self._reader.readline()
                except (ConnectionError, asyncio.IncompleteReadError):
                    break

                if not line:
                    # EOF
                    break

                # Strip trailing newline
                line = line.rstrip(b"\n").rstrip(b"\r")

                if not line:
                    continue

                if len(line) > MAX_LINE_LENGTH:
                    logger.warning(
                        "Replication line too long (%d bytes), closing", len(line)
                    )
                    break

                try:
                    await self._parse_and_dispatch_line(line)
                except Exception:
                    logger.exception(
                        "Error processing replication line: %s", line[:200]
                    )
        except asyncio.CancelledError:
            pass
        finally:
            await self.close()

    async def _parse_and_dispatch_line(self, line: bytes) -> None:
        """Parse a line into a command and dispatch it."""
        decoded = line.decode("utf-8")

        cmd = parse_command_from_line(decoded)

        if cmd.NAME not in self._valid_inbound:
            logger.warning(
                "Received unexpected command %s from %s", cmd.NAME, self.conn_id
            )
            return

        self._last_received_command = time.time()

        await self.handle_command(cmd)

    async def handle_command(self, cmd: Command) -> None:
        """Dispatch a command to the appropriate handler.

        First checks for on_<COMMAND_NAME> on this protocol instance,
        then on the command_handler.
        """
        handler_name = f"on_{cmd.NAME}"

        # Protocol-level handler
        handler = getattr(self, handler_name, None)
        if handler:
            result = handler(cmd)
            if isinstance(result, Awaitable):
                await result

        # Business-logic handler
        if self._command_handler:
            handler = getattr(self._command_handler, handler_name, None)
            if handler:
                result = handler(cmd)
                if isinstance(result, Awaitable):
                    await result

    async def send_command(self, cmd: Command) -> None:
        """Send a command over the wire.

        If the connection is not yet established, buffers the command.
        """
        if self._state == ConnectionState.CLOSED:
            return

        if self._state == ConnectionState.CONNECTING:
            self._pending_commands.append(cmd)
            if len(self._pending_commands) > MAX_PENDING_COMMANDS:
                logger.error(
                    "Replication command buffer overflow (%d), closing %s",
                    len(self._pending_commands),
                    self.conn_id,
                )
                await self.close()
            return

        line = f"{cmd.NAME} {cmd.to_line()}"
        if "\n" in line:
            raise ValueError(f"Replication command contains newline: {line!r}")

        encoded = line.encode("utf-8")
        if len(encoded) > MAX_LINE_LENGTH:
            raise ValueError(
                f"Replication command too long ({len(encoded)} bytes)"
            )

        assert self._writer is not None
        self._writer.write(encoded + b"\n")
        try:
            await self._writer.drain()
        except ConnectionError:
            await self.close()
            return

        self._last_sent_command = time.time()

    async def _send_pending_commands(self) -> None:
        """Drain the pending command buffer."""
        pending = self._pending_commands
        self._pending_commands = []
        for cmd in pending:
            await self.send_command(cmd)

    async def _ping_loop(self) -> None:
        """Periodically check connection health and send pings."""
        try:
            while self._state != ConnectionState.CLOSED:
                await asyncio.sleep(PING_INTERVAL)

                now = time.time()

                if self._time_we_closed is not None:
                    # We're in graceful shutdown — wait for PING_TIMEOUT then abort
                    if now - self._time_we_closed > PING_TIMEOUT:
                        logger.warning(
                            "Replication connection %s didn't close cleanly, aborting",
                            self.conn_id,
                        )
                        self._force_close()
                    continue

                # Send ping if we haven't sent anything recently
                if now - self._last_sent_command > PING_INTERVAL:
                    await self.send_command(
                        PingCommand(str(int(now * 1000)))
                    )

                # Check for timeout (only after first ping received)
                if self._received_ping:
                    if now - self._last_received_command > PING_TIMEOUT:
                        logger.warning(
                            "Replication connection %s timed out (no data for %.1fs)",
                            self.conn_id,
                            now - self._last_received_command,
                        )
                        await self.close()
        except asyncio.CancelledError:
            pass

    def on_PING(self, cmd: Command) -> None:
        """Handle incoming PING — enables timeout mechanism."""
        self._received_ping = True

    async def close(self) -> None:
        """Gracefully close the connection."""
        if self._state == ConnectionState.CLOSED:
            return

        self._state = ConnectionState.CLOSED
        self._time_we_closed = time.time()

        logger.info("Closing replication connection: %s", self.conn_id)

        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

        await self.on_connection_lost()

    def _force_close(self) -> None:
        """Force close without waiting."""
        self._state = ConnectionState.CLOSED
        if self._writer:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()

    # --- Subclass hooks ---

    async def on_connection_made(self) -> None:
        """Called when the connection is established. Override in subclasses."""
        pass

    async def on_connection_lost(self) -> None:
        """Called when the connection is lost. Override in subclasses."""
        pass


async def start_native_replication_server(
    host: str,
    port: int,
    protocol_factory: Callable[[], NativeReplicationProtocol],
) -> asyncio.Server:
    """Start an asyncio-based replication server.

    This is the asyncio-native equivalent of listening with
    ReplicationStreamProtocolFactory.

    Args:
        host: Host to bind to.
        port: Port to bind to.
        protocol_factory: Callable that creates a new NativeReplicationProtocol
            for each incoming connection.

    Returns:
        The asyncio.Server instance.
    """

    async def _handle_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        protocol = protocol_factory()
        await protocol.start(reader, writer)
        # Wait for the read loop to finish
        if protocol._read_task:
            try:
                await protocol._read_task
            except asyncio.CancelledError:
                pass

    server = await asyncio.start_server(_handle_connection, host, port)
    logger.info("Replication server listening on %s:%d", host, port)
    return server


async def connect_native_replication_client(
    host: str,
    port: int,
    protocol_factory: Callable[[], NativeReplicationProtocol],
    reconnect_interval: float = 5.0,
) -> asyncio.Task[None]:
    """Connect to a replication server with automatic reconnection.

    This is the asyncio-native equivalent of using ReconnectingClientFactory.

    Args:
        host: Server host.
        port: Server port.
        protocol_factory: Creates a new protocol for each connection.
        reconnect_interval: Seconds between reconnection attempts.

    Returns:
        The background task managing the connection.
    """

    async def _connect_loop() -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, port)
                protocol = protocol_factory()
                await protocol.start(reader, writer)
                # Wait for the read loop to complete (connection lost)
                if protocol._read_task:
                    await protocol._read_task
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning(
                    "Replication connection to %s:%d failed, retrying in %.1fs",
                    host,
                    port,
                    reconnect_interval,
                    exc_info=True,
                )

            await asyncio.sleep(reconnect_interval)

    return asyncio.create_task(_connect_loop())
