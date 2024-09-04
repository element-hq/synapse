#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020, 2022 The Matrix.org Foundation C.I.C.
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
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Deque,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from prometheus_client import Counter

from twisted.internet.protocol import ReconnectingClientFactory

from synapse.metrics import LaterGauge
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.replication.tcp.commands import (
    ClearUserSyncsCommand,
    Command,
    FederationAckCommand,
    LockReleasedCommand,
    NewActiveTaskCommand,
    PositionCommand,
    RdataCommand,
    RemoteServerUpCommand,
    ReplicateCommand,
    UserIpCommand,
    UserSyncCommand,
)
from synapse.replication.tcp.context import ClientContextFactory
from synapse.replication.tcp.protocol import IReplicationConnection
from synapse.replication.tcp.streams import (
    STREAMS_MAP,
    AccountDataStream,
    BackfillStream,
    CachesStream,
    EventsStream,
    FederationStream,
    PresenceFederationStream,
    PresenceStream,
    PushRulesStream,
    ReceiptsStream,
    Stream,
    ToDeviceStream,
    TypingStream,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# number of updates received for each RDATA stream
inbound_rdata_count = Counter(
    "synapse_replication_tcp_protocol_inbound_rdata_count", "", ["stream_name"]
)
user_sync_counter = Counter("synapse_replication_tcp_resource_user_sync", "")
federation_ack_counter = Counter("synapse_replication_tcp_resource_federation_ack", "")
remove_pusher_counter = Counter("synapse_replication_tcp_resource_remove_pusher", "")

user_ip_cache_counter = Counter("synapse_replication_tcp_resource_user_ip_cache", "")


# the type of the entries in _command_queues_by_stream
_StreamCommandQueue = Deque[
    Tuple[Union[RdataCommand, PositionCommand], IReplicationConnection]
]


class ReplicationCommandHandler:
    """Handles incoming commands from replication as well as sending commands
    back out to connections.
    """

    def __init__(self, hs: "HomeServer"):
        self._replication_data_handler = hs.get_replication_data_handler()
        self._presence_handler = hs.get_presence_handler()
        self._store = hs.get_datastores().main
        self._notifier = hs.get_notifier()
        self._clock = hs.get_clock()
        self._instance_id = hs.get_instance_id()
        self._instance_name = hs.get_instance_name()

        # Additional Redis channel suffixes to subscribe to.
        self._channels_to_subscribe_to: List[str] = []

        self._is_presence_writer = (
            hs.get_instance_name() in hs.config.worker.writers.presence
        )

        self._streams: Dict[str, Stream] = {
            stream.NAME: stream(hs) for stream in STREAMS_MAP.values()
        }

        # List of streams that this instance is the source of
        self._streams_to_replicate: List[Stream] = []

        for stream in self._streams.values():
            if hs.config.redis.redis_enabled and stream.NAME == CachesStream.NAME:
                # All workers can write to the cache invalidation stream when
                # using redis.
                self._streams_to_replicate.append(stream)
                continue

            if isinstance(stream, (EventsStream, BackfillStream)):
                # Only add EventStream and BackfillStream as a source on the
                # instance in charge of event persistence.
                if hs.get_instance_name() in hs.config.worker.writers.events:
                    self._streams_to_replicate.append(stream)

                continue

            if isinstance(stream, ToDeviceStream):
                # Only add ToDeviceStream as a source on instances in charge of
                # sending to device messages.
                if hs.get_instance_name() in hs.config.worker.writers.to_device:
                    self._streams_to_replicate.append(stream)

                continue

            if isinstance(stream, TypingStream):
                # Only add TypingStream as a source on the instance in charge of
                # typing.
                if hs.get_instance_name() in hs.config.worker.writers.typing:
                    self._streams_to_replicate.append(stream)

                continue

            if isinstance(stream, AccountDataStream):
                # Only add AccountDataStream and TagAccountDataStream as a source on the
                # instance in charge of account_data persistence.
                if hs.get_instance_name() in hs.config.worker.writers.account_data:
                    self._streams_to_replicate.append(stream)

                continue

            if isinstance(stream, ReceiptsStream):
                # Only add ReceiptsStream as a source on the instance in charge of
                # receipts.
                if hs.get_instance_name() in hs.config.worker.writers.receipts:
                    self._streams_to_replicate.append(stream)

                continue

            if isinstance(stream, (PresenceStream, PresenceFederationStream)):
                # Only add PresenceStream as a source on the instance in charge
                # of presence.
                if self._is_presence_writer:
                    self._streams_to_replicate.append(stream)

                continue

            if isinstance(stream, PushRulesStream):
                if hs.get_instance_name() in hs.config.worker.writers.push_rules:
                    self._streams_to_replicate.append(stream)

                continue

            # Only add any other streams if we're on master.
            if hs.config.worker.worker_app is not None:
                continue

            if (
                stream.NAME == FederationStream.NAME
                and hs.config.worker.send_federation
            ):
                # We only support federation stream if federation sending
                # has been disabled on the master.
                continue

            self._streams_to_replicate.append(stream)

        # Map of stream name to batched updates. See RdataCommand for info on
        # how batching works.
        self._pending_batches: Dict[str, List[Any]] = {}

        # The factory used to create connections.
        self._factory: Optional[ReconnectingClientFactory] = None

        # The currently connected connections. (The list of places we need to send
        # outgoing replication commands to.)
        self._connections: List[IReplicationConnection] = []

        LaterGauge(
            "synapse_replication_tcp_resource_total_connections",
            "",
            [],
            lambda: len(self._connections),
        )

        # When POSITION or RDATA commands arrive, we stick them in a queue and process
        # them in order in a separate background process.

        # the streams which are currently being processed by _unsafe_process_queue
        self._processing_streams: Set[str] = set()

        # for each stream, a queue of commands that are awaiting processing, and the
        # connection that they arrived on.
        self._command_queues_by_stream = {
            stream_name: _StreamCommandQueue() for stream_name in self._streams
        }

        # For each connection, the incoming stream names that have received a POSITION
        # from that connection.
        self._streams_by_connection: Dict[IReplicationConnection, Set[str]] = {}

        LaterGauge(
            "synapse_replication_tcp_command_queue",
            "Number of inbound RDATA/POSITION commands queued for processing",
            ["stream_name"],
            lambda: {
                (stream_name,): len(queue)
                for stream_name, queue in self._command_queues_by_stream.items()
            },
        )

        self._is_master = hs.config.worker.worker_app is None

        self._federation_sender = None
        if self._is_master and not hs.config.worker.send_federation:
            self._federation_sender = hs.get_federation_sender()

        self._server_notices_sender = None
        if self._is_master:
            self._server_notices_sender = hs.get_server_notices_sender()

        self._task_scheduler = None
        if hs.config.worker.run_background_tasks:
            self._task_scheduler = hs.get_task_scheduler()

        if hs.config.redis.redis_enabled:
            # If we're using Redis, it's the background worker that should
            # receive USER_IP commands and store the relevant client IPs.
            self._should_insert_client_ips = hs.config.worker.run_background_tasks
        else:
            # If we're NOT using Redis, this must be handled by the master
            self._should_insert_client_ips = hs.get_instance_name() == "master"

        if self._is_master or self._should_insert_client_ips:
            self.subscribe_to_channel("USER_IP")

        if hs.config.redis.redis_enabled:
            self._notifier.add_lock_released_callback(self.on_lock_released)

        # Marks if we should send POSITION commands for all streams ASAP. This
        # is checked by the `ReplicationStreamer` which manages sending
        # RDATA/POSITION commands
        self._should_announce_positions = True

    def subscribe_to_channel(self, channel_name: str) -> None:
        """
        Indicates that we wish to subscribe to a Redis channel by name.

        (The name will later be prefixed with the server name; i.e. subscribing
        to the 'ABC' channel actually subscribes to 'example.com/ABC' Redis-side.)

        Raises:
          - If replication has already started, then it's too late to subscribe
            to new channels.
        """

        if self._factory is not None:
            # We don't allow subscribing after the fact to avoid the chance
            # of missing an important message because we didn't subscribe in time.
            raise RuntimeError(
                "Cannot subscribe to more channels after replication started."
            )

        if channel_name not in self._channels_to_subscribe_to:
            self._channels_to_subscribe_to.append(channel_name)

    def _add_command_to_stream_queue(
        self, conn: IReplicationConnection, cmd: Union[RdataCommand, PositionCommand]
    ) -> None:
        """Queue the given received command for processing

        Adds the given command to the per-stream queue, and processes the queue if
        necessary
        """
        stream_name = cmd.stream_name
        queue = self._command_queues_by_stream.get(stream_name)
        if queue is None:
            logger.error("Got %s for unknown stream: %s", cmd.NAME, stream_name)
            return

        queue.append((cmd, conn))

        # if we're already processing this stream, there's nothing more to do:
        # the new entry on the queue will get picked up in due course
        if stream_name in self._processing_streams:
            return

        # fire off a background process to start processing the queue.
        run_as_background_process(
            "process-replication-data", self._unsafe_process_queue, stream_name
        )

    async def _unsafe_process_queue(self, stream_name: str) -> None:
        """Processes the command queue for the given stream, until it is empty

        Does not check if there is already a thread processing the queue, hence "unsafe"
        """
        assert stream_name not in self._processing_streams

        self._processing_streams.add(stream_name)
        try:
            queue = self._command_queues_by_stream.get(stream_name)
            while queue:
                cmd, conn = queue.popleft()
                try:
                    await self._process_command(cmd, conn, stream_name)
                except Exception:
                    logger.exception("Failed to handle command %s", cmd)
        finally:
            self._processing_streams.discard(stream_name)

    async def _process_command(
        self,
        cmd: Union[PositionCommand, RdataCommand],
        conn: IReplicationConnection,
        stream_name: str,
    ) -> None:
        if isinstance(cmd, PositionCommand):
            await self._process_position(stream_name, conn, cmd)
        elif isinstance(cmd, RdataCommand):
            await self._process_rdata(stream_name, conn, cmd)
        else:
            # This shouldn't be possible
            raise Exception("Unrecognised command %s in stream queue", cmd.NAME)

    def start_replication(self, hs: "HomeServer") -> None:
        """Helper method to start replication."""
        from synapse.replication.tcp.redis import RedisDirectTcpReplicationClientFactory

        # First let's ensure that we have a ReplicationStreamer started.
        hs.get_replication_streamer()

        # We need two connections to redis, one for the subscription stream and
        # one to send commands to (as you can't send further redis commands to a
        # connection after SUBSCRIBE is called).

        # First create the connection for sending commands.
        outbound_redis_connection = hs.get_outbound_redis_connection()

        # Now create the factory/connection for the subscription stream.
        self._factory = RedisDirectTcpReplicationClientFactory(
            hs,
            outbound_redis_connection,
            channel_names=self._channels_to_subscribe_to,
        )

        reactor = hs.get_reactor()
        redis_config = hs.config.redis
        if redis_config.redis_path is not None:
            reactor.connectUNIX(
                redis_config.redis_path,
                self._factory,
                timeout=30,
                checkPID=False,
            )

        elif hs.config.redis.redis_use_tls:
            ssl_context_factory = ClientContextFactory(hs.config.redis)
            reactor.connectSSL(
                redis_config.redis_host,
                redis_config.redis_port,
                self._factory,
                ssl_context_factory,
                timeout=30,
                bindAddress=None,
            )
        else:
            reactor.connectTCP(
                redis_config.redis_host,
                redis_config.redis_port,
                self._factory,
                timeout=30,
                bindAddress=None,
            )

    def get_streams(self) -> Dict[str, Stream]:
        """Get a map from stream name to all streams."""
        return self._streams

    def get_streams_to_replicate(self) -> List[Stream]:
        """Get a list of streams that this instances replicates."""
        return self._streams_to_replicate

    def on_REPLICATE(self, conn: IReplicationConnection, cmd: ReplicateCommand) -> None:
        self.send_positions_to_connection()

    def send_positions_to_connection(self) -> None:
        """Send current position of all streams this process is source of to
        the connection.
        """

        self._should_announce_positions = True
        self._notifier.notify_replication()

    def should_announce_positions(self) -> bool:
        """Check if we should send POSITION commands for all streams ASAP."""
        return self._should_announce_positions

    def will_announce_positions(self) -> None:
        """Mark that we're about to send POSITIONs out for all streams."""
        self._should_announce_positions = False

    def on_USER_SYNC(
        self, conn: IReplicationConnection, cmd: UserSyncCommand
    ) -> Optional[Awaitable[None]]:
        user_sync_counter.inc()

        if self._is_presence_writer:
            return self._presence_handler.update_external_syncs_row(
                cmd.instance_id,
                cmd.user_id,
                cmd.device_id,
                cmd.is_syncing,
                cmd.last_sync_ms,
            )
        else:
            return None

    def on_CLEAR_USER_SYNC(
        self, conn: IReplicationConnection, cmd: ClearUserSyncsCommand
    ) -> Optional[Awaitable[None]]:
        if self._is_presence_writer:
            return self._presence_handler.update_external_syncs_clear(cmd.instance_id)
        else:
            return None

    def on_FEDERATION_ACK(
        self, conn: IReplicationConnection, cmd: FederationAckCommand
    ) -> None:
        federation_ack_counter.inc()

        if self._federation_sender:
            self._federation_sender.federation_ack(cmd.instance_name, cmd.token)

    def on_USER_IP(
        self, conn: IReplicationConnection, cmd: UserIpCommand
    ) -> Optional[Awaitable[None]]:
        user_ip_cache_counter.inc()

        if self._is_master or self._should_insert_client_ips:
            # We make a point of only returning an awaitable if there's actually
            # something to do; on_USER_IP is not an async function, but
            # _handle_user_ip is.
            # If on_USER_IP returns an awaitable, it gets scheduled as a
            # background process (see `BaseReplicationStreamProtocol.handle_command`).
            return self._handle_user_ip(cmd)
        else:
            # Returning None when this process definitely has nothing to do
            # reduces the overhead of handling the USER_IP command, which is
            # currently broadcast to all workers regardless of utility.
            return None

    async def _handle_user_ip(self, cmd: UserIpCommand) -> None:
        """
        Handles a User IP, branching depending on whether we are the main process
        and/or the background worker.
        """
        if self._is_master:
            assert self._server_notices_sender is not None
            await self._server_notices_sender.on_user_ip(cmd.user_id)

        if self._should_insert_client_ips:
            await self._store.insert_client_ip(
                cmd.user_id,
                cmd.access_token,
                cmd.ip,
                cmd.user_agent,
                cmd.device_id,
                cmd.last_seen,
            )

    def on_RDATA(self, conn: IReplicationConnection, cmd: RdataCommand) -> None:
        if cmd.instance_name == self._instance_name:
            # Ignore RDATA that are just our own echoes
            return

        stream_name = cmd.stream_name
        inbound_rdata_count.labels(stream_name).inc()

        # We put the received command into a queue here for two reasons:
        #   1. so we don't try and concurrently handle multiple rows for the
        #      same stream, and
        #   2. so we don't race with getting a POSITION command and fetching
        #      missing RDATA.

        self._add_command_to_stream_queue(conn, cmd)

    async def _process_rdata(
        self, stream_name: str, conn: IReplicationConnection, cmd: RdataCommand
    ) -> None:
        """Process an RDATA command

        Called after the command has been popped off the queue of inbound commands
        """
        try:
            row = STREAMS_MAP[stream_name].parse_row(cmd.row)
        except Exception as e:
            raise Exception(
                "Failed to parse RDATA: %r %r" % (stream_name, cmd.row)
            ) from e

        # make sure that we've processed a POSITION for this stream *on this
        # connection*. (A POSITION on another connection is no good, as there
        # is no guarantee that we have seen all the intermediate updates.)
        sbc = self._streams_by_connection.get(conn)
        if not sbc or stream_name not in sbc:
            # Let's drop the row for now, on the assumption we'll receive a
            # `POSITION` soon and we'll catch up correctly then.
            logger.debug(
                "Discarding RDATA for unconnected stream %s -> %s",
                stream_name,
                cmd.token,
            )
            return

        if cmd.token is None:
            # I.e. this is part of a batch of updates for this stream (in
            # which case batch until we get an update for the stream with a non
            # None token).
            self._pending_batches.setdefault(stream_name, []).append(row)
            return

        # Check if this is the last of a batch of updates
        rows = self._pending_batches.pop(stream_name, [])
        rows.append(row)

        stream = self._streams[stream_name]

        # Find where we previously streamed up to.
        current_token = stream.current_token(cmd.instance_name)

        # Discard this data if this token is earlier than the current
        # position. Note that streams can be reset (in which case you
        # expect an earlier token), but that must be preceded by a
        # POSITION command.
        if cmd.token <= current_token:
            logger.debug(
                "Discarding RDATA from stream %s at position %s before previous position %s",
                stream_name,
                cmd.token,
                current_token,
            )
        else:
            await self.on_rdata(stream_name, cmd.instance_name, cmd.token, rows)

    async def on_rdata(
        self, stream_name: str, instance_name: str, token: int, rows: list
    ) -> None:
        """Called to handle a batch of replication data with a given stream token.

        Args:
            stream_name: name of the replication stream for this batch of rows
            instance_name: the instance that wrote the rows.
            token: stream token for this batch of rows
            rows: a list of Stream.ROW_TYPE objects as returned by
                Stream.parse_row.
        """
        logger.debug("Received rdata %s (%s) -> %s", stream_name, instance_name, token)
        await self._replication_data_handler.on_rdata(
            stream_name, instance_name, token, rows
        )

    def on_POSITION(self, conn: IReplicationConnection, cmd: PositionCommand) -> None:
        if cmd.instance_name == self._instance_name:
            # Ignore POSITION that are just our own echoes
            return

        logger.debug("Handling '%s %s'", cmd.NAME, cmd.to_line())

        # Check if we can early discard this position. We can only do so for
        # connected streams.
        stream = self._streams[cmd.stream_name]
        if stream.can_discard_position(
            cmd.instance_name, cmd.prev_token, cmd.new_token
        ) and self.is_stream_connected(conn, cmd.stream_name):
            logger.debug(
                "Discarding redundant POSITION %s/%s %s %s",
                cmd.instance_name,
                cmd.stream_name,
                cmd.prev_token,
                cmd.new_token,
            )
            return

        self._add_command_to_stream_queue(conn, cmd)

    async def _process_position(
        self, stream_name: str, conn: IReplicationConnection, cmd: PositionCommand
    ) -> None:
        """Process a POSITION command

        Called after the command has been popped off the queue of inbound commands
        """
        stream = self._streams[stream_name]

        if stream.can_discard_position(
            cmd.instance_name, cmd.prev_token, cmd.new_token
        ) and self.is_stream_connected(conn, cmd.stream_name):
            logger.debug(
                "Discarding redundant POSITION %s/%s %s %s",
                cmd.instance_name,
                cmd.stream_name,
                cmd.prev_token,
                cmd.new_token,
            )
            return

        # We're about to go and catch up with the stream, so remove from set
        # of connected streams.
        for streams in self._streams_by_connection.values():
            streams.discard(stream_name)

        # We clear the pending batches for the stream as the fetching of the
        # missing updates below will fetch all rows in the batch.
        self._pending_batches.pop(stream_name, [])

        # Find where we previously streamed up to.
        current_token = stream.current_token(cmd.instance_name)

        # If the incoming previous position is less than our current position
        # then we're up to date and there's nothing to do. Otherwise, fetch
        # all updates between then and now.
        #
        # Note: We also have to check that `current_token` is at most the
        # new position, to handle the case where the stream gets "reset"
        # (e.g. for `caches` and `typing` after the writer's restart).
        missing_updates = not (cmd.prev_token <= current_token <= cmd.new_token)
        while missing_updates:
            # Note: There may very well not be any new updates, but we check to
            # make sure. This can particularly happen for the event stream where
            # event persisters continuously send `POSITION`. See `resource.py`
            # for why this can happen.

            logger.info(
                "Fetching replication rows for '%s' / %s between %i and %i",
                stream_name,
                cmd.instance_name,
                current_token,
                cmd.new_token,
            )
            (updates, current_token, missing_updates) = await stream.get_updates_since(
                cmd.instance_name, current_token, cmd.new_token
            )

            # TODO: add some tests for this

            # Some streams return multiple rows with the same stream IDs,
            # which need to be processed in batches.

            for token, rows in _batch_updates(updates):
                await self.on_rdata(
                    stream_name,
                    cmd.instance_name,
                    token,
                    [stream.parse_row(row) for row in rows],
                )

            logger.info("Caught up with stream '%s' to %i", stream_name, current_token)

        # We've now caught up to position sent to us, notify handler.
        await self._replication_data_handler.on_position(
            cmd.stream_name, cmd.instance_name, cmd.new_token
        )

        self._streams_by_connection.setdefault(conn, set()).add(stream_name)

    def is_stream_connected(
        self, conn: IReplicationConnection, stream_name: str
    ) -> bool:
        """Return if stream has been successfully connected and is ready to
        receive updates"""
        return stream_name in self._streams_by_connection.get(conn, ())

    def on_REMOTE_SERVER_UP(
        self, conn: IReplicationConnection, cmd: RemoteServerUpCommand
    ) -> None:
        """Called when get a new REMOTE_SERVER_UP command."""
        self._notifier.notify_remote_server_up(cmd.data)

    def on_LOCK_RELEASED(
        self, conn: IReplicationConnection, cmd: LockReleasedCommand
    ) -> None:
        """Called when we get a new LOCK_RELEASED command."""
        if cmd.instance_name == self._instance_name:
            return

        self._notifier.notify_lock_released(
            cmd.instance_name, cmd.lock_name, cmd.lock_key
        )

    def on_NEW_ACTIVE_TASK(
        self, conn: IReplicationConnection, cmd: NewActiveTaskCommand
    ) -> None:
        """Called when get a new NEW_ACTIVE_TASK command."""
        if self._task_scheduler:
            self._task_scheduler.launch_task_by_id(cmd.data)

    def new_connection(self, connection: IReplicationConnection) -> None:
        """Called when we have a new connection."""
        self._connections.append(connection)

        # If we are connected to replication as a client (rather than a server)
        # we need to reset the reconnection delay on the client factory (which
        # is used to do exponential back off when the connection drops).
        #
        # Ideally we would reset the delay when we've "fully established" the
        # connection (for some definition thereof) to stop us from tightlooping
        # on reconnection if something fails after this point and we drop the
        # connection. Unfortunately, we don't really have a better definition of
        # "fully established" than the connection being established.
        if self._factory:
            self._factory.resetDelay()

        # Tell the other end if we have any users currently syncing.
        currently_syncing = (
            self._presence_handler.get_currently_syncing_users_for_replication()
        )

        now = self._clock.time_msec()
        for user_id, device_id in currently_syncing:
            connection.send_command(
                UserSyncCommand(self._instance_id, user_id, device_id, True, now)
            )

    def lost_connection(self, connection: IReplicationConnection) -> None:
        """Called when a connection is closed/lost."""
        # we no longer need _streams_by_connection for this connection.
        streams = self._streams_by_connection.pop(connection, None)
        if streams:
            logger.info(
                "Lost replication connection; streams now disconnected: %s", streams
            )
        try:
            self._connections.remove(connection)
        except ValueError:
            pass

    def connected(self) -> bool:
        """Do we have any replication connections open?

        Is used by e.g. `ReplicationStreamer` to no-op if nothing is connected.
        """
        return bool(self._connections)

    def send_command(self, cmd: Command) -> None:
        """Send a command to all connected connections.

        Args:
            cmd
        """
        if self._connections:
            for connection in self._connections:
                try:
                    connection.send_command(cmd)
                except Exception:
                    # We probably want to catch some types of exceptions here
                    # and log them as warnings (e.g. connection gone), but I
                    # can't find what those exception types they would be.
                    logger.exception(
                        "Failed to write command %s to connection %s",
                        cmd.NAME,
                        connection,
                    )
        else:
            logger.warning("Dropping command as not connected: %r", cmd.NAME)

    def send_federation_ack(self, token: int) -> None:
        """Ack data for the federation stream. This allows the master to drop
        data stored purely in memory.
        """
        self.send_command(FederationAckCommand(self._instance_name, token))

    def send_user_sync(
        self,
        instance_id: str,
        user_id: str,
        device_id: Optional[str],
        is_syncing: bool,
        last_sync_ms: int,
    ) -> None:
        """Poke the master that a user has started/stopped syncing."""
        self.send_command(
            UserSyncCommand(instance_id, user_id, device_id, is_syncing, last_sync_ms)
        )

    def send_user_ip(
        self,
        user_id: str,
        access_token: str,
        ip: str,
        user_agent: str,
        device_id: Optional[str],
        last_seen: int,
    ) -> None:
        """Tell the master that the user made a request."""
        cmd = UserIpCommand(user_id, access_token, ip, user_agent, device_id, last_seen)
        self.send_command(cmd)

    def send_remote_server_up(self, server: str) -> None:
        self.send_command(RemoteServerUpCommand(server))

    def stream_update(self, stream_name: str, token: Optional[int], data: Any) -> None:
        """Called when a new update is available to stream to Redis subscribers.

        We need to check if the client is interested in the stream or not
        """
        self.send_command(RdataCommand(stream_name, self._instance_name, token, data))

    def on_lock_released(
        self, instance_name: str, lock_name: str, lock_key: str
    ) -> None:
        """Called when we released a lock and should notify other instances."""
        if instance_name == self._instance_name:
            self.send_command(LockReleasedCommand(instance_name, lock_name, lock_key))

    def send_new_active_task(self, task_id: str) -> None:
        """Called when a new task has been scheduled for immediate launch and is ACTIVE."""
        self.send_command(NewActiveTaskCommand(task_id))


UpdateToken = TypeVar("UpdateToken")
UpdateRow = TypeVar("UpdateRow")


def _batch_updates(
    updates: Iterable[Tuple[UpdateToken, UpdateRow]],
) -> Iterator[Tuple[UpdateToken, List[UpdateRow]]]:
    """Collect stream updates with the same token together

    Given a series of updates returned by Stream.get_updates_since(), collects
    the updates which share the same stream_id together.

    For example:

        [(1, a), (1, b), (2, c), (3, d), (3, e)]

    becomes:

        [
            (1, [a, b]),
            (2, [c]),
            (3, [d, e]),
        ]
    """

    update_iter = iter(updates)

    first_update = next(update_iter, None)
    if first_update is None:
        # empty input
        return

    current_batch_token = first_update[0]
    current_batch = [first_update[1]]

    for token, row in update_iter:
        if token != current_batch_token:
            # different token to the previous row: flush the previous
            # batch and start anew
            yield current_batch_token, current_batch
            current_batch_token = token
            current_batch = []

        current_batch.append(row)

    # flush the final batch
    yield current_batch_token, current_batch
