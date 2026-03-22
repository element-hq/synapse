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
import ssl
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Optional

import redis.asyncio

try:
    from zope.interface import implementer
except ImportError:
    def implementer(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        def decorator(cls: Any) -> Any:
            return cls
        return decorator

from synapse.logging.context import PreserveLoggingContext
from synapse.metrics import SERVER_NAME_LABEL
from synapse.metrics.background_process_metrics import (
    BackgroundProcessLoggingContext,
    wrap_as_background_process,
)
from synapse.replication.tcp.commands import (
    Command,
    ReplicateCommand,
    parse_command_from_line,
)
from synapse.replication.tcp.context import ClientContextFactory
from synapse.replication.tcp.protocol import (
    IReplicationConnection,
    tcp_inbound_commands_counter,
    tcp_outbound_commands_counter,
)
from synapse.util.duration import Duration

if TYPE_CHECKING:
    from synapse.replication.tcp.handler import ReplicationCommandHandler
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@implementer(IReplicationConnection)
class RedisSubscriber:
    """Async Redis subscriber for replication.

    This class fulfils two functions:

    (a) it manages a redis pub/sub subscription, parsing *incoming* messages into
    replication commands, and passing them to `ReplicationCommandHandler`

    (b) it implements the IReplicationConnection API, where it sends *outgoing* commands
    onto the outbound redis connection.

    Attributes:
        server_name: The homeserver name of the Synapse instance.
        hs: The HomeServer instance.
        synapse_handler: The command handler to handle incoming commands.
        synapse_stream_prefix: The redis stream name prefix.
        synapse_channel_names: Additional channel name suffixes.
        synapse_outbound_redis_connection: The redis.asyncio.Redis connection for publishing.
    """

    def __init__(
        self,
        hs: "HomeServer",
        outbound_redis_connection: redis.asyncio.Redis,
        channel_names: list[str],
    ):
        self.server_name = hs.hostname
        self.hs = hs
        self.synapse_handler: "ReplicationCommandHandler" = (
            hs.get_replication_command_handler()
        )
        self.synapse_stream_prefix = hs.hostname
        self.synapse_channel_names = channel_names
        self.synapse_outbound_redis_connection = outbound_redis_connection

        self._pubsub: Optional[redis.asyncio.client.PubSub] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._logging_context: Optional[BackgroundProcessLoggingContext] = None

    def _get_logging_context(self) -> BackgroundProcessLoggingContext:
        if self._logging_context is None:
            with PreserveLoggingContext():
                self._logging_context = BackgroundProcessLoggingContext(
                    name="replication_command_handler", server_name=self.server_name
                )
        return self._logging_context

    async def start(self, pubsub: redis.asyncio.client.PubSub) -> None:
        """Start the subscription and listen for messages."""
        self._pubsub = pubsub

        fully_qualified_stream_names = [
            f"{self.synapse_stream_prefix}/{stream_suffix}"
            for stream_suffix in self.synapse_channel_names
        ] + [self.synapse_stream_prefix]

        logger.info("Sending redis SUBSCRIBE for %r", fully_qualified_stream_names)
        await self._pubsub.subscribe(*fully_qualified_stream_names)

        logger.info(
            "Successfully subscribed to redis stream, sending REPLICATE command"
        )
        self.synapse_handler.new_connection(self)
        await self._async_send_command(ReplicateCommand())
        logger.info("REPLICATE successfully sent")

        # We send out our positions when there is a new connection in case the
        # other side missed updates.
        self.synapse_handler.send_positions_to_connection()

        # Start the background listener
        self._listen_task = asyncio.ensure_future(self._listen_loop())

    async def _listen_loop(self) -> None:
        """Background loop that reads messages from the pub/sub subscription."""
        assert self._pubsub is not None
        try:
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    with PreserveLoggingContext(self._get_logging_context()):
                        self._parse_and_dispatch_message(data)
        except redis.asyncio.ConnectionError:
            logger.info("Lost connection to redis (subscriber)")
        except asyncio.CancelledError:
            logger.info("Redis subscriber listen loop cancelled")
        except Exception:
            logger.exception("Unexpected error in redis subscriber listen loop")
        finally:
            self._on_connection_lost()

    def _on_connection_lost(self) -> None:
        """Handle a lost connection."""
        logger.info("Lost connection to redis")
        self.synapse_handler.lost_connection(self)

        # mark the logging context as finished
        with PreserveLoggingContext():
            with self._get_logging_context():
                pass

    def _parse_and_dispatch_message(self, message: str) -> None:
        if message.strip() == "":
            return

        try:
            cmd = parse_command_from_line(message)
        except Exception:
            logger.exception(
                "Failed to parse replication line: %r",
                message,
            )
            return

        tcp_inbound_commands_counter.labels(
            command=cmd.NAME,
            name="redis",
            **{SERVER_NAME_LABEL: self.server_name},
        ).inc()

        self.handle_command(cmd)

    def handle_command(self, cmd: Command) -> None:
        """Handle a command received over the replication stream."""
        cmd_func = getattr(self.synapse_handler, "on_%s" % (cmd.NAME,), None)
        if not cmd_func:
            logger.warning("Unhandled command: %r", cmd)
            return

        res = cmd_func(self, cmd)

        if isawaitable(res):
            self.hs.run_as_background_process(
                "replication-" + cmd.get_logcontext_id(), lambda: res
            )

    def send_command(self, cmd: Command) -> None:
        """Send a command if connection has been established."""
        self.hs.run_as_background_process(
            "send-cmd",
            self._async_send_command,
            cmd,
            bg_start_span=False,
        )

    async def _async_send_command(self, cmd: Command) -> None:
        """Encode a replication command and send it over our outbound connection."""
        string = "%s %s" % (cmd.NAME, cmd.to_line())
        if "\n" in string:
            raise Exception("Unexpected newline in command: %r", string)

        encoded_string = string.encode("utf-8")

        tcp_outbound_commands_counter.labels(
            command=cmd.NAME,
            name="redis",
            **{SERVER_NAME_LABEL: self.server_name},
        ).inc()

        channel_name = cmd.redis_channel_name(self.synapse_stream_prefix)

        await self.synapse_outbound_redis_connection.publish(
            channel_name, encoded_string
        )

    async def stop(self) -> None:
        """Stop the subscriber and clean up."""
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._pubsub is not None:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()


class RedisReplicationManager:
    """Manages Redis connections for replication with automatic reconnection.

    This replaces the old Twisted-based SynapseRedisFactory and
    RedisDirectTcpReplicationClientFactory.
    """

    # Reconnection parameters
    INITIAL_RETRY_DELAY = 1.0
    MAX_RETRY_DELAY = 5.0

    def __init__(
        self,
        hs: "HomeServer",
        outbound_redis_connection: redis.asyncio.Redis,
        channel_names: list[str],
    ):
        self.hs = hs
        self.server_name = hs.hostname
        self._outbound_redis_connection = outbound_redis_connection
        self._channel_names = channel_names
        self._subscriber: Optional[RedisSubscriber] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._stopped = False

        # Start periodic pings on the outbound connection
        hs.get_clock().looping_call(self._send_ping, Duration(seconds=30))

    @wrap_as_background_process("redis_ping")
    async def _send_ping(self) -> None:
        try:
            await self._outbound_redis_connection.ping()
        except Exception:
            logger.warning("Failed to send ping to redis connection")

    async def start(self) -> None:
        """Start the replication manager, connecting to Redis."""
        await self._connect()

    async def _connect(self) -> None:
        """Create a subscriber and connect to Redis."""
        retry_delay = self.INITIAL_RETRY_DELAY

        while not self._stopped:
            try:
                subscriber = RedisSubscriber(
                    self.hs,
                    self._outbound_redis_connection,
                    self._channel_names,
                )

                # Create a new Redis connection for the pub/sub subscriber
                sub_connection = _create_redis_connection_from_config(self.hs)
                pubsub = sub_connection.pubsub()

                await subscriber.start(pubsub)
                self._subscriber = subscriber
                logger.info("Connected to redis for replication")

                # Reset retry delay on successful connection
                retry_delay = self.INITIAL_RETRY_DELAY

                # Wait for the listen task to finish (which means we disconnected)
                if subscriber._listen_task is not None:
                    await subscriber._listen_task

                logger.info("Redis subscriber disconnected, will reconnect")

            except redis.asyncio.ConnectionError as e:
                logger.info(
                    "Connection to redis failed: %s, retrying in %.1fs",
                    e,
                    retry_delay,
                )
            except Exception:
                logger.exception(
                    "Unexpected error connecting to redis, retrying in %.1fs",
                    retry_delay,
                )

            if self._stopped:
                break

            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, self.MAX_RETRY_DELAY)

    async def stop(self) -> None:
        """Stop the replication manager."""
        self._stopped = True
        if self._subscriber is not None:
            await self._subscriber.stop()
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()


def _create_redis_connection_from_config(hs: "HomeServer") -> redis.asyncio.Redis:
    """Create a redis.asyncio.Redis connection from the HomeServer config."""
    rc = hs.config.redis

    ssl_context: Optional[ssl.SSLContext] = None
    if rc.redis_use_tls:
        ctx_factory = ClientContextFactory(rc)
        ssl_context = ctx_factory.getContext()

    if rc.redis_path is not None:
        return redis.asyncio.Redis(
            unix_socket_path=rc.redis_path,
            password=rc.redis_password,
            db=rc.redis_dbid or 0,
        )
    else:
        return redis.asyncio.Redis(
            host=rc.redis_host,
            port=rc.redis_port,
            password=rc.redis_password,
            db=rc.redis_dbid or 0,
            ssl=ssl_context is not None,
            ssl_ca_certs=rc.redis_ca_file if rc.redis_use_tls else None,
            ssl_ca_data=None,
            ssl_certfile=rc.redis_certificate if rc.redis_use_tls else None,
            ssl_keyfile=rc.redis_private_key if rc.redis_use_tls else None,
        )


def create_redis_connection(
    hs: "HomeServer",
    host: str = "localhost",
    port: int = 6379,
    password: Optional[str] = None,
    dbid: Optional[int] = None,
) -> redis.asyncio.Redis:
    """Create a redis.asyncio.Redis connection for outbound commands (publish, ping, etc.)."""
    rc = hs.config.redis

    ssl_context: Optional[ssl.SSLContext] = None
    if rc.redis_use_tls:
        ctx_factory = ClientContextFactory(rc)
        ssl_context = ctx_factory.getContext()

    return redis.asyncio.Redis(
        host=host,
        port=port,
        password=password,
        db=dbid or 0,
        ssl=ssl_context is not None,
        ssl_ca_certs=rc.redis_ca_file if rc.redis_use_tls else None,
        ssl_certfile=rc.redis_certificate if rc.redis_use_tls else None,
        ssl_keyfile=rc.redis_private_key if rc.redis_use_tls else None,
    )


def create_redis_unix_connection(
    hs: "HomeServer",
    path: str = "/tmp/redis.sock",
    password: Optional[str] = None,
    dbid: Optional[int] = None,
) -> redis.asyncio.Redis:
    """Create a redis.asyncio.Redis connection over a Unix socket."""
    return redis.asyncio.Redis(
        unix_socket_path=path,
        password=password,
        db=dbid or 0,
    )
