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
import logging
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

import attr

from synapse.replication.tcp.streams._base import ThreadSubscriptionsStream
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.util.caches.descriptors import cached

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ThreadSubscription:
    automatic: bool
    """
    whether the subscription was made automatically (as opposed to by manual
    action from the user)
    """


class ThreadSubscriptionsWorkerStore(CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._can_write_to_thread_subscriptions = (
            self._instance_name in hs.config.worker.writers.thread_subscriptions
        )

        self._thread_subscriptions_id_gen: MultiWriterIdGenerator = (
            MultiWriterIdGenerator(
                db_conn=db_conn,
                db=database,
                notifier=hs.get_replication_notifier(),
                stream_name="thread_subscriptions",
                instance_name=self._instance_name,
                tables=[
                    ("thread_subscriptions", "instance_name", "stream_id"),
                ],
                sequence_name="thread_subscriptions_sequence",
                writers=hs.config.worker.writers.thread_subscriptions,
            )
        )

    def process_replication_rows(
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        if stream_name == ThreadSubscriptionsStream.NAME:
            for row in rows:
                self.get_subscription_for_thread.invalidate(
                    (row.user_id, row.room_id, row.event_id)
                )

        super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == ThreadSubscriptionsStream.NAME:
            self._thread_subscriptions_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    async def subscribe_user_to_thread(
        self, user_id: str, room_id: str, thread_root_event_id: str, *, automatic: bool
    ) -> Optional[int]:
        """Updates a user's subscription settings for a specific thread root.

        If no change would be made to the subscription, does not produce any database change.

        Args:
            user_id: The ID of the user whose settings are being updated.
            room_id: The ID of the room the thread root belongs to.
            thread_root_event_id: The event ID of the thread root.
            automatic: Whether the subscription was performed automatically by the user's client.
                Only `False` will overwrite an existing value of automatic for a subscription row.

        Returns:
            The stream ID for this update, if the update isn't no-opped.
        """
        assert self._can_write_to_thread_subscriptions

        def _subscribe_user_to_thread_txn(txn: LoggingTransaction) -> Optional[int]:
            already_automatic = self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={
                    "user_id": user_id,
                    "event_id": thread_root_event_id,
                    "room_id": room_id,
                    "subscribed": True,
                },
                retcol="automatic",
                allow_none=True,
            )

            if already_automatic is None:
                already_subscribed = False
                already_automatic = True
            else:
                already_subscribed = True
                # convert int (SQLite bool) to Python bool
                already_automatic = bool(already_automatic)

            if already_subscribed and already_automatic == automatic:
                # there is nothing we need to do here
                return None

            stream_id = self._thread_subscriptions_id_gen.get_next_txn(txn)

            values: Dict[str, Optional[Union[bool, int, str]]] = {
                "subscribed": True,
                "stream_id": stream_id,
                "instance_name": self._instance_name,
                "automatic": already_automatic and automatic,
            }

            self.db_pool.simple_upsert_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={
                    "user_id": user_id,
                    "event_id": thread_root_event_id,
                    "room_id": room_id,
                },
                values=values,
            )

            txn.call_after(
                self.get_subscription_for_thread.invalidate,
                (user_id, room_id, thread_root_event_id),
            )

            return stream_id

        return await self.db_pool.runInteraction(
            "subscribe_user_to_thread", _subscribe_user_to_thread_txn
        )

    async def unsubscribe_user_from_thread(
        self, user_id: str, room_id: str, thread_root_event_id: str
    ) -> Optional[int]:
        """Unsubscribes a user from a thread.

        If no change would be made to the subscription, does not produce any database change.

        Args:
            user_id: The ID of the user whose settings are being updated.
            room_id: The ID of the room the thread root belongs to.
            thread_root_event_id: The event ID of the thread root.

        Returns:
            The stream ID for this update, if the update isn't no-opped.
        """

        assert self._can_write_to_thread_subscriptions

        def _unsubscribe_user_from_thread_txn(txn: LoggingTransaction) -> Optional[int]:
            already_subscribed = self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={
                    "user_id": user_id,
                    "event_id": thread_root_event_id,
                    "room_id": room_id,
                },
                retcol="subscribed",
                allow_none=True,
            )

            if already_subscribed is None or already_subscribed is False:
                # there is nothing we need to do here
                return None

            stream_id = self._thread_subscriptions_id_gen.get_next_txn(txn)

            self.db_pool.simple_update_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={
                    "user_id": user_id,
                    "event_id": thread_root_event_id,
                    "room_id": room_id,
                    "subscribed": True,
                },
                updatevalues={
                    "subscribed": False,
                    "stream_id": stream_id,
                    "instance_name": self._instance_name,
                },
            )

            txn.call_after(
                self.get_subscription_for_thread.invalidate,
                (user_id, room_id, thread_root_event_id),
            )

            return stream_id

        return await self.db_pool.runInteraction(
            "unsubscribe_user_from_thread", _unsubscribe_user_from_thread_txn
        )

    async def purge_thread_subscription_settings_for_user(self, user_id: str) -> None:
        """
        Purge all subscriptions for the user.
        The fact that subscriptions have been purged will not be streamed;
        all stream rows for the user will in fact be removed.
        This is intended only for dealing with user deactivation.
        """

        def _purge_thread_subscription_settings_for_user_txn(
            txn: LoggingTransaction,
        ) -> None:
            self.db_pool.simple_delete_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={"user_id": user_id},
            )
            self._invalidate_cache_and_stream(
                txn, self.get_subscription_for_thread, (user_id,)
            )

        await self.db_pool.runInteraction(
            desc="purge_thread_subscription_settings_for_user",
            func=_purge_thread_subscription_settings_for_user_txn,
        )

    @cached(tree=True)
    async def get_subscription_for_thread(
        self, user_id: str, room_id: str, thread_root_event_id: str
    ) -> Optional[ThreadSubscription]:
        """Get the thread subscription for a specific thread and user.

        Args:
            user_id: The ID of the user
            room_id: The ID of the room
            thread_root_event_id: The event ID of the thread root

        Returns:
            A `ThreadSubscription` dataclass if there is a subscription,
            or `None` if there is no subscription.

            If there is a row in the table but `subscribed` is `False`,
            behaves the same as if there was no row at all and returns `None`.
        """
        row = await self.db_pool.simple_select_one(
            table="thread_subscriptions",
            keyvalues={
                "user_id": user_id,
                "room_id": room_id,
                "event_id": thread_root_event_id,
                "subscribed": True,
            },
            retcols=("automatic",),
            allow_none=True,
            desc="get_subscription_for_thread",
        )

        if row is None:
            return None

        (automatic_rawbool,) = row

        # convert SQLite integer booleans into real booleans
        automatic = bool(automatic_rawbool)

        return ThreadSubscription(automatic=automatic)

    def get_max_thread_subscriptions_stream_id(self) -> int:
        """Get the current maximum stream_id for thread subscriptions.

        Returns:
            The maximum stream_id
        """
        return self._thread_subscriptions_id_gen.get_current_token()

    async def get_updated_thread_subscriptions(
        self, from_id: int, to_id: int, limit: int
    ) -> List[Tuple[int, str, str, str]]:
        """Get updates to thread subscriptions between two stream IDs.

        Args:
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            list of (stream_id, user_id, room_id, thread_root_id) tuples
        """

        def get_updated_thread_subscriptions_txn(
            txn: LoggingTransaction,
        ) -> List[Tuple[int, str, str, str]]:
            sql = """
                SELECT stream_id, user_id, room_id, event_id
                FROM thread_subscriptions
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
            """

            txn.execute(sql, (from_id, to_id, limit))
            return cast(List[Tuple[int, str, str, str]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_updated_thread_subscriptions",
            get_updated_thread_subscriptions_txn,
        )

    async def get_updated_thread_subscriptions_for_user(
        self, user_id: str, from_id: int, to_id: int, limit: int
    ) -> List[Tuple[int, str, str]]:
        """Get updates to thread subscriptions for a specific user.

        Args:
            user_id: The ID of the user
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return

        Returns:
            A list of (stream_id, room_id, thread_root_event_id) tuples.
        """

        def get_updated_thread_subscriptions_for_user_txn(
            txn: LoggingTransaction,
        ) -> List[Tuple[int, str, str]]:
            sql = """
                SELECT stream_id, room_id, event_id
                FROM thread_subscriptions
                WHERE user_id = ? AND ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
            """

            txn.execute(sql, (user_id, from_id, to_id, limit))
            return [(row[0], row[1], row[2]) for row in txn]

        return await self.db_pool.runInteraction(
            "get_updated_thread_subscriptions_for_user",
            get_updated_thread_subscriptions_for_user_txn,
        )
