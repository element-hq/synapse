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
    Iterable,
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
from synapse.types import EventOrderings
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


class AutomaticSubscriptionConflicted:
    """
    Marker return value to signal that an automatic subscription was skipped,
    because it conflicted with an unsubscription that we consider to have
    been made later than the event causing the automatic subscription.
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
                server_name=self.server_name,
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
                self.get_subscribers_to_thread.invalidate((row.room_id, row.event_id))

        super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == ThreadSubscriptionsStream.NAME:
            self._thread_subscriptions_id_gen.advance(instance_name, token)
        super().process_replication_position(stream_name, instance_name, token)

    @staticmethod
    def _should_skip_autosubscription_after_unsubscription(
        *,
        autosub: EventOrderings,
        unsubscribed_at: EventOrderings,
    ) -> bool:
        """
        Returns whether an automatic subscription occurring *after* an unsubscription
        should be skipped, because the unsubscription already 'acknowledges' the event
        causing the automatic subscription (the cause event).

        To determine *after*, we use `stream_ordering` unless the event is backfilled
        (negative `stream_ordering`) and fallback to topological ordering.

        Args:
            autosub: the stream_ordering and topological_ordering of the cause event
            unsubscribed_at:
                the maximum stream ordering and the maximum topological ordering at the time of unsubscription

        Returns:
            True if the automatic subscription should be skipped
        """
        # For normal rooms, these two orderings should be positive, because
        # they don't refer to a specific event but rather the maximum at the
        # time of unsubscription.
        #
        # However, for rooms that have never been joined and that are being peeked at,
        # we might not have a single non-backfilled event and therefore the stream
        # ordering might be negative, so we don't assert this case.
        assert unsubscribed_at.topological > 0

        unsubscribed_at_backfilled = unsubscribed_at.stream < 0
        if (
            not unsubscribed_at_backfilled
            and unsubscribed_at.stream >= autosub.stream > 0
        ):
            # non-backfilled events: the unsubscription is later according to
            # the stream
            return True

        if autosub.stream < 0:
            # the auto-subscription cause event was backfilled, so fall back to
            # topological ordering
            if unsubscribed_at.topological >= autosub.topological:
                return True

        return False

    async def subscribe_user_to_thread(
        self,
        user_id: str,
        room_id: str,
        thread_root_event_id: str,
        *,
        automatic_event_orderings: EventOrderings | None,
    ) -> int | AutomaticSubscriptionConflicted | None:
        """Updates a user's subscription settings for a specific thread root.

        If no change would be made to the subscription, does not produce any database change.

        Case-by-case:
            - if we already have an automatic subscription:
                - new automatic subscriptions will be no-ops (no database write),
                - new manual subscriptions will overwrite the automatic subscription
            - if we already have a manual subscription:
              we don't update (no database write) in either case, because:
                - the existing manual subscription wins over a new automatic subscription request
                - there would be no need to write a manual subscription because we already have one

        Args:
            user_id: The ID of the user whose settings are being updated.
            room_id: The ID of the room the thread root belongs to.
            thread_root_event_id: The event ID of the thread root.
            automatic_event_orderings:
                Value depends on whether the subscription was performed automatically by the user's client.
                For manual subscriptions: None.
                For automatic subscriptions: the orderings of the event.

        Returns:
            If a subscription is made: (int) the stream ID for this update.
            If a subscription already exists and did not need to be updated: None
            If an automatic subscription conflicted with an unsubscription: AutomaticSubscriptionConflicted
        """
        assert self._can_write_to_thread_subscriptions

        def _invalidate_subscription_caches(txn: LoggingTransaction) -> None:
            txn.call_after(
                self.get_subscription_for_thread.invalidate,
                (user_id, room_id, thread_root_event_id),
            )
            txn.call_after(
                self.get_subscribers_to_thread.invalidate,
                (room_id, thread_root_event_id),
            )

        def _subscribe_user_to_thread_txn(
            txn: LoggingTransaction,
        ) -> int | AutomaticSubscriptionConflicted | None:
            requested_automatic = automatic_event_orderings is not None

            row = self.db_pool.simple_select_one_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={
                    "user_id": user_id,
                    "event_id": thread_root_event_id,
                    "room_id": room_id,
                },
                retcols=(
                    "subscribed",
                    "automatic",
                    "unsubscribed_at_stream_ordering",
                    "unsubscribed_at_topological_ordering",
                ),
                allow_none=True,
            )

            if row is None:
                # We have never subscribed before, simply insert the row and finish
                stream_id = self._thread_subscriptions_id_gen.get_next_txn(txn)
                self.db_pool.simple_insert_txn(
                    txn,
                    table="thread_subscriptions",
                    values={
                        "user_id": user_id,
                        "event_id": thread_root_event_id,
                        "room_id": room_id,
                        "subscribed": True,
                        "stream_id": stream_id,
                        "instance_name": self._instance_name,
                        "automatic": requested_automatic,
                        "unsubscribed_at_stream_ordering": None,
                        "unsubscribed_at_topological_ordering": None,
                    },
                )
                _invalidate_subscription_caches(txn)
                return stream_id

            # we already have either a subscription or a prior unsubscription here
            (
                subscribed,
                already_automatic,
                unsubscribed_at_stream_ordering,
                unsubscribed_at_topological_ordering,
            ) = row

            if subscribed and (not already_automatic or requested_automatic):
                # we are already subscribed and the current subscription state
                # is good enough (either we already have a manual subscription,
                # or we requested an automatic subscription)
                # In that case, nothing to change here.
                # (See docstring for case-by-case explanation)
                return None

            if not subscribed and requested_automatic:
                assert automatic_event_orderings is not None
                # we previously unsubscribed and we are now automatically subscribing
                # Check whether the new autosubscription should be skipped
                if ThreadSubscriptionsWorkerStore._should_skip_autosubscription_after_unsubscription(
                    autosub=automatic_event_orderings,
                    unsubscribed_at=EventOrderings(
                        unsubscribed_at_stream_ordering,
                        unsubscribed_at_topological_ordering,
                    ),
                ):
                    # skip the subscription
                    return AutomaticSubscriptionConflicted()

            # At this point: we have now finished checking that we need to make
            # a subscription, updating the current row.

            stream_id = self._thread_subscriptions_id_gen.get_next_txn(txn)
            self.db_pool.simple_update_txn(
                txn,
                table="thread_subscriptions",
                keyvalues={
                    "user_id": user_id,
                    "event_id": thread_root_event_id,
                    "room_id": room_id,
                },
                updatevalues={
                    "subscribed": True,
                    "stream_id": stream_id,
                    "instance_name": self._instance_name,
                    "automatic": requested_automatic,
                    "unsubscribed_at_stream_ordering": None,
                    "unsubscribed_at_topological_ordering": None,
                },
            )
            _invalidate_subscription_caches(txn)

            return stream_id

        return await self.db_pool.runInteraction(
            "subscribe_user_to_thread", _subscribe_user_to_thread_txn
        )

    async def unsubscribe_user_from_thread(
        self, user_id: str, room_id: str, thread_root_event_id: str
    ) -> int | None:
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

        def _unsubscribe_user_from_thread_txn(txn: LoggingTransaction) -> int | None:
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

            # Find the maximum stream ordering and topological ordering of the room,
            # which we then store against this unsubscription so we can skip future
            # automatic subscriptions that are caused by an event logically earlier
            # than this unsubscription.
            txn.execute(
                """
                SELECT MAX(stream_ordering) AS mso, MAX(topological_ordering) AS mto FROM events
                WHERE room_id = ?
                """,
                (room_id,),
            )
            ord_row = txn.fetchone()
            assert ord_row is not None
            max_stream_ordering, max_topological_ordering = ord_row

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
                    "unsubscribed_at_stream_ordering": max_stream_ordering,
                    "unsubscribed_at_topological_ordering": max_topological_ordering,
                },
            )

            txn.call_after(
                self.get_subscription_for_thread.invalidate,
                (user_id, room_id, thread_root_event_id),
            )
            txn.call_after(
                self.get_subscribers_to_thread.invalidate,
                (room_id, thread_root_event_id),
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

        This must only be used for user deactivation,
        because it does not invalidate the `subscribers_to_thread` cache.
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
    ) -> ThreadSubscription | None:
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

    # max_entries=100 rationale:
    # this returns a potentially large datastructure
    # (since each entry contains a set which contains a potentially large number of user IDs),
    # whereas the default of 10'000 entries for @cached feels more
    # suitable for very small cache entries.
    #
    # Overall, when bearing in mind the usual profile of a small community-server or company-server
    # (where cache tuning hasn't been done, so we're in out-of-box configuration), it is very
    # unlikely we would benefit from keeping hot the subscribers for as many as 100 threads,
    # since it's unlikely that so many threads will be active in a short span of time on a small homeserver.
    # It feels that medium servers will probably also not exhaust this limit.
    # Larger homeservers are more likely to be carefully tuned, either with a larger global cache factor
    # or carefully following the usage patterns & cache metrics.
    # Finally, the query is not so intensive that computing it every time is a huge deal, but given people
    # often send messages back-to-back in the same thread it seems like it would offer a mild benefit.
    @cached(max_entries=100)
    async def get_subscribers_to_thread(
        self, room_id: str, thread_root_event_id: str
    ) -> frozenset[str]:
        """
        Returns:
            the set of user_ids for local users who are subscribed to the given thread.
        """
        return frozenset(
            await self.db_pool.simple_select_onecol(
                table="thread_subscriptions",
                keyvalues={
                    "room_id": room_id,
                    "event_id": thread_root_event_id,
                    "subscribed": True,
                },
                retcol="user_id",
                desc="get_subscribers_to_thread",
            )
        )

    def get_max_thread_subscriptions_stream_id(self) -> int:
        """Get the current maximum stream_id for thread subscriptions.

        Returns:
            The maximum stream_id
        """
        return self._thread_subscriptions_id_gen.get_current_token()

    def get_thread_subscriptions_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._thread_subscriptions_id_gen

    async def get_updated_thread_subscriptions(
        self, *, from_id: int, to_id: int, limit: int
    ) -> list[tuple[int, str, str, str]]:
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
        ) -> list[tuple[int, str, str, str]]:
            sql = """
                SELECT stream_id, user_id, room_id, event_id
                FROM thread_subscriptions
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
            """

            txn.execute(sql, (from_id, to_id, limit))
            return cast(list[tuple[int, str, str, str]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_updated_thread_subscriptions",
            get_updated_thread_subscriptions_txn,
        )

    async def get_latest_updated_thread_subscriptions_for_user(
        self, user_id: str, *, from_id: int, to_id: int, limit: int
    ) -> list[tuple[int, str, str, bool, bool | None]]:
        """Get the latest updates to thread subscriptions for a specific user.

        Args:
            user_id: The ID of the user
            from_id: The starting stream ID (exclusive)
            to_id: The ending stream ID (inclusive)
            limit: The maximum number of rows to return
                If there are too many rows to return, rows from the start (closer to `from_id`)
                will be omitted.

        Returns:
            A list of (stream_id, room_id, thread_root_event_id, subscribed, automatic) tuples.
            The row with lowest `stream_id` is the first row.
        """

        def get_updated_thread_subscriptions_for_user_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[int, str, str, bool, bool | None]]:
            sql = """
                WITH the_updates AS (
                    SELECT stream_id, room_id, event_id, subscribed, automatic
                    FROM thread_subscriptions
                    WHERE user_id = ? AND ? < stream_id AND stream_id <= ?
                    ORDER BY stream_id DESC
                    LIMIT ?
                )
                SELECT stream_id, room_id, event_id, subscribed, automatic
                FROM the_updates
                ORDER BY stream_id ASC
            """

            txn.execute(sql, (user_id, from_id, to_id, limit))
            return [
                (
                    stream_id,
                    room_id,
                    event_id,
                    # SQLite integer to boolean conversions
                    bool(subscribed),
                    bool(automatic) if subscribed else None,
                )
                for (stream_id, room_id, event_id, subscribed, automatic) in txn
            ]

        return await self.db_pool.runInteraction(
            "get_updated_thread_subscriptions_for_user",
            get_updated_thread_subscriptions_for_user_txn,
        )
