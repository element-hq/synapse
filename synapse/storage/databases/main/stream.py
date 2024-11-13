#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2017 Vector Creations Ltd
# Copyright 2014-2016 OpenMarket Ltd
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

"""This module is responsible for getting events from the DB for pagination
and event streaming.

The order it returns events in depend on whether we are streaming forwards or
are paginating backwards. We do this because we want to handle out of order
messages nicely, while still returning them in the correct order when we
paginate bacwards.

This is implemented by keeping two ordering columns: stream_ordering and
topological_ordering. Stream ordering is basically insertion/received order
(except for events from backfill requests). The topological_ordering is a
weak ordering of events based on the pdu graph.

This means that we have to have two different types of tokens, depending on
what sort order was used:
    - stream tokens are of the form: "s%d", which maps directly to the column
    - topological tokems: "t%d-%d", where the integers map to the topological
      and stream ordering columns respectively.
"""

import logging
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Set,
    Tuple,
    cast,
    overload,
)

import attr
from immutabledict import immutabledict
from typing_extensions import Literal, assert_never

from twisted.internet import defer

from synapse.api.constants import Direction, EventTypes, Membership
from synapse.api.filtering import Filter
from synapse.events import EventBase
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.opentracing import tag_args, trace
from synapse.storage._base import SQLBaseStore
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_in_list_sql_clause,
)
from synapse.storage.databases.main.events_worker import EventsWorkerStore
from synapse.storage.engines import BaseDatabaseEngine, PostgresEngine, Sqlite3Engine
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import PersistedEventPosition, RoomStreamToken, StrCollection
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.cancellation import cancellable
from synapse.util.iterutils import batch_iter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


MAX_STREAM_SIZE = 1000


_STREAM_TOKEN = "stream"
_TOPOLOGICAL_TOKEN = "topological"


class PaginateFunction(Protocol):
    async def __call__(
        self,
        *,
        room_id: str,
        from_key: RoomStreamToken,
        to_key: Optional[RoomStreamToken] = None,
        direction: Direction = Direction.BACKWARDS,
        limit: int = 0,
    ) -> Tuple[List[EventBase], RoomStreamToken, bool]: ...


# Used as return values for pagination APIs
@attr.s(slots=True, frozen=True, auto_attribs=True)
class _EventDictReturn:
    event_id: str
    topological_ordering: Optional[int]
    stream_ordering: int


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _EventsAround:
    events_before: List[EventBase]
    events_after: List[EventBase]
    start: RoomStreamToken
    end: RoomStreamToken


@attr.s(slots=True, frozen=True, auto_attribs=True)
class CurrentStateDeltaMembership:
    """
    Attributes:
        event_id: The "current" membership event ID in this room.
        event_pos: The position of the "current" membership event in the event stream.
        prev_event_id: The previous membership event in this room that was replaced by
            the "current" one. May be `None` if there was no previous membership event.
        room_id: The room ID of the membership event.
        membership: The membership state of the user in the room
        sender: The person who sent the membership event
    """

    room_id: str
    # Event
    event_id: Optional[str]
    event_pos: PersistedEventPosition
    membership: str
    sender: Optional[str]
    # Prev event
    prev_event_id: Optional[str]
    prev_event_pos: Optional[PersistedEventPosition]
    prev_membership: Optional[str]
    prev_sender: Optional[str]


def generate_pagination_where_clause(
    direction: Direction,
    column_names: Tuple[str, str],
    from_token: Optional[Tuple[Optional[int], int]],
    to_token: Optional[Tuple[Optional[int], int]],
    engine: BaseDatabaseEngine,
) -> str:
    """Creates an SQL expression to bound the columns by the pagination
    tokens.

    For example creates an SQL expression like:

        (6, 7) >= (topological_ordering, stream_ordering)
        AND (5, 3) < (topological_ordering, stream_ordering)

    would be generated for dir=b, from_token=(6, 7) and to_token=(5, 3).

    Note that tokens are considered to be after the row they are in, e.g. if
    a row A has a token T, then we consider A to be before T. This convention
    is important when figuring out inequalities for the generated SQL, and
    produces the following result:
        - If paginating forwards then we exclude any rows matching the from
          token, but include those that match the to token.
        - If paginating backwards then we include any rows matching the from
          token, but include those that match the to token.

    Args:
        direction: Whether we're paginating backwards or forwards.
        column_names: The column names to bound. Must *not* be user defined as
            these get inserted directly into the SQL statement without escapes.
        from_token: The start point for the pagination. This is an exclusive
            minimum bound if direction is forwards, and an inclusive maximum bound if
            direction is backwards.
        to_token: The endpoint point for the pagination. This is an inclusive
            maximum bound if direction is forwards, and an exclusive minimum bound if
            direction is backwards.
        engine: The database engine to generate the clauses for

    Returns:
        The sql expression
    """

    where_clause = []
    if from_token:
        where_clause.append(
            _make_generic_sql_bound(
                bound=">=" if direction == Direction.BACKWARDS else "<",
                column_names=column_names,
                values=from_token,
                engine=engine,
            )
        )

    if to_token:
        where_clause.append(
            _make_generic_sql_bound(
                bound="<" if direction == Direction.BACKWARDS else ">=",
                column_names=column_names,
                values=to_token,
                engine=engine,
            )
        )

    return " AND ".join(where_clause)


def generate_pagination_bounds(
    direction: Direction,
    from_token: Optional[RoomStreamToken],
    to_token: Optional[RoomStreamToken],
) -> Tuple[
    str, Optional[Tuple[Optional[int], int]], Optional[Tuple[Optional[int], int]]
]:
    """
    Generate a start and end point for this page of events.

    Args:
        direction: Whether pagination is going forwards or backwards.
        from_token: The token to start pagination at, or None to start at the first value.
        to_token: The token to end pagination at, or None to not limit the end point.

    Returns:
        A three tuple of:

            ASC or DESC for sorting of the query.

            The starting position as a tuple of ints representing
            (topological position, stream position) or None if no from_token was
            provided. The topological position may be None for live tokens.

            The end position in the same format as the starting position, or None
            if no to_token was provided.
    """

    # Tokens really represent positions between elements, but we use
    # the convention of pointing to the event before the gap. Hence
    # we have a bit of asymmetry when it comes to equalities.
    if direction == Direction.BACKWARDS:
        order = "DESC"
    else:
        order = "ASC"

    # The bounds for the stream tokens are complicated by the fact
    # that we need to handle the instance_map part of the tokens. We do this
    # by fetching all events between the min stream token and the maximum
    # stream token (as returned by `RoomStreamToken.get_max_stream_pos`) and
    # then filtering the results.
    from_bound: Optional[Tuple[Optional[int], int]] = None
    if from_token:
        if from_token.topological is not None:
            from_bound = from_token.as_historical_tuple()
        elif direction == Direction.BACKWARDS:
            from_bound = (
                None,
                from_token.get_max_stream_pos(),
            )
        else:
            from_bound = (
                None,
                from_token.stream,
            )

    to_bound: Optional[Tuple[Optional[int], int]] = None
    if to_token:
        if to_token.topological is not None:
            to_bound = to_token.as_historical_tuple()
        elif direction == Direction.BACKWARDS:
            to_bound = (
                None,
                to_token.stream,
            )
        else:
            to_bound = (
                None,
                to_token.get_max_stream_pos(),
            )

    return order, from_bound, to_bound


def generate_next_token(
    direction: Direction, last_topo_ordering: Optional[int], last_stream_ordering: int
) -> RoomStreamToken:
    """
    Generate the next room stream token based on the currently returned data.

    Args:
        direction: Whether pagination is going forwards or backwards.
        last_topo_ordering: The last topological ordering being returned.
        last_stream_ordering: The last stream ordering being returned.

    Returns:
        A new RoomStreamToken to return to the client.
    """
    if direction == Direction.BACKWARDS:
        # Tokens are positions between events.
        # This token points *after* the last event in the chunk.
        # We need it to point to the event before it in the chunk
        # when we are going backwards so we subtract one from the
        # stream part.
        last_stream_ordering -= 1
    return RoomStreamToken(topological=last_topo_ordering, stream=last_stream_ordering)


def _make_generic_sql_bound(
    bound: str,
    column_names: Tuple[str, str],
    values: Tuple[Optional[int], int],
    engine: BaseDatabaseEngine,
) -> str:
    """Create an SQL expression that bounds the given column names by the
    values, e.g. create the equivalent of `(1, 2) < (col1, col2)`.

    Only works with two columns.

    Older versions of SQLite don't support that syntax so we have to expand it
    out manually.

    Args:
        bound: The comparison operator to use. One of ">", "<", ">=",
            "<=", where the values are on the left and columns on the right.
        names: The column names. Must *not* be user defined
            as these get inserted directly into the SQL statement without
            escapes.
        values: The values to bound the columns by. If
            the first value is None then only creates a bound on the second
            column.
        engine: The database engine to generate the SQL for

    Returns:
        The SQL statement
    """

    assert bound in (">", "<", ">=", "<=")

    name1, name2 = column_names
    val1, val2 = values

    if val1 is None:
        val2 = int(val2)
        return "(%d %s %s)" % (val2, bound, name2)

    val1 = int(val1)
    val2 = int(val2)

    if isinstance(engine, PostgresEngine):
        # Postgres doesn't optimise ``(x < a) OR (x=a AND y<b)`` as well
        # as it optimises ``(x,y) < (a,b)`` on multicolumn indexes. So we
        # use the later form when running against postgres.
        return "((%d,%d) %s (%s,%s))" % (val1, val2, bound, name1, name2)

    # We want to generate queries of e.g. the form:
    #
    #   (val1 < name1 OR (val1 = name1 AND val2 <= name2))
    #
    # which is equivalent to (val1, val2) < (name1, name2)

    return """(
        {val1:d} {strict_bound} {name1}
        OR ({val1:d} = {name1} AND {val2:d} {bound} {name2})
    )""".format(
        name1=name1,
        val1=val1,
        name2=name2,
        val2=val2,
        strict_bound=bound[0],  # The first bound must always be strict equality here
        bound=bound,
    )


def _filter_results(
    lower_token: Optional[RoomStreamToken],
    upper_token: Optional[RoomStreamToken],
    instance_name: Optional[str],
    topological_ordering: int,
    stream_ordering: int,
) -> bool:
    """Returns True if the event persisted by the given instance at the given
    topological/stream_ordering falls between the two tokens (taking a None
    token to mean unbounded).

    Used to filter results from fetching events in the DB against the given
    tokens. This is necessary to handle the case where the tokens include
    position maps, which we handle by fetching more than necessary from the DB
    and then filtering (rather than attempting to construct a complicated SQL
    query).

    The `instance_name` arg is optional to handle historic rows, and is
    interpreted as if it was "master".
    """

    if instance_name is None:
        instance_name = "master"

    event_historical_tuple = (
        topological_ordering,
        stream_ordering,
    )

    if lower_token:
        if lower_token.topological is not None:
            # If these are historical tokens we compare the `(topological, stream)`
            # tuples.
            if event_historical_tuple <= lower_token.as_historical_tuple():
                return False

        else:
            # If these are live tokens we compare the stream ordering against the
            # writers stream position.
            if stream_ordering <= lower_token.get_stream_pos_for_instance(
                instance_name
            ):
                return False

    if upper_token:
        if upper_token.topological is not None:
            if upper_token.as_historical_tuple() < event_historical_tuple:
                return False
        else:
            if upper_token.get_stream_pos_for_instance(instance_name) < stream_ordering:
                return False

    return True


def _filter_results_by_stream(
    lower_token: Optional[RoomStreamToken],
    upper_token: Optional[RoomStreamToken],
    instance_name: Optional[str],
    stream_ordering: int,
) -> bool:
    """
    This function only works with "live" tokens with `stream_ordering` only. See
    `_filter_results(...)` if you want to work with all tokens.

    Returns True if the event persisted by the given instance at the given
    stream_ordering falls between the two tokens (taking a None
    token to mean unbounded).

    Used to filter results from fetching events in the DB against the given
    tokens. This is necessary to handle the case where the tokens include
    position maps, which we handle by fetching more than necessary from the DB
    and then filtering (rather than attempting to construct a complicated SQL
    query).

    The `instance_name` arg is optional to handle historic rows, and is
    interpreted as if it was "master".
    """
    if instance_name is None:
        instance_name = "master"

    if lower_token:
        assert lower_token.topological is None

        # If these are live tokens we compare the stream ordering against the
        # writers stream position.
        if stream_ordering <= lower_token.get_stream_pos_for_instance(instance_name):
            return False

    if upper_token:
        assert upper_token.topological is None

        if upper_token.get_stream_pos_for_instance(instance_name) < stream_ordering:
            return False

    return True


def filter_to_clause(event_filter: Optional[Filter]) -> Tuple[str, List[str]]:
    # NB: This may create SQL clauses that don't optimise well (and we don't
    # have indices on all possible clauses). E.g. it may create
    # "room_id == X AND room_id != X", which postgres doesn't optimise.

    if not event_filter:
        return "", []

    clauses = []
    args = []

    # Handle event types with potential wildcard characters
    if event_filter.types:
        type_clauses = []
        for typ in event_filter.types:
            if "*" in typ:
                type_clauses.append("event.type LIKE ?")
                typ = typ.replace("*", "%")  # Replace * with % for SQL LIKE pattern
            else:
                type_clauses.append("event.type = ?")
            args.append(typ)
        clauses.append("(%s)" % " OR ".join(type_clauses))

    # Handle event types to exclude with potential wildcard characters
    for typ in event_filter.not_types:
        if "*" in typ:
            clauses.append("event.type NOT LIKE ?")
            typ = typ.replace("*", "%")
        else:
            clauses.append("event.type != ?")
        args.append(typ)

    if event_filter.senders:
        clauses.append(
            "(%s)" % " OR ".join("event.sender = ?" for _ in event_filter.senders)
        )
        args.extend(event_filter.senders)

    for sender in event_filter.not_senders:
        clauses.append("event.sender != ?")
        args.append(sender)

    if event_filter.rooms:
        clauses.append(
            "(%s)" % " OR ".join("event.room_id = ?" for _ in event_filter.rooms)
        )
        args.extend(event_filter.rooms)

    for room_id in event_filter.not_rooms:
        clauses.append("event.room_id != ?")
        args.append(room_id)

    if event_filter.contains_url:
        clauses.append("event.contains_url = ?")
        args.append(event_filter.contains_url)

    # We're only applying the "labels" filter on the database query, because applying the
    # "not_labels" filter via a SQL query is non-trivial. Instead, we let
    # event_filter.check_fields apply it, which is not as efficient but makes the
    # implementation simpler.
    if event_filter.labels:
        clauses.append("(%s)" % " OR ".join("label = ?" for _ in event_filter.labels))
        args.extend(event_filter.labels)

    # Filter on relation_senders / relation types from the joined tables.
    if event_filter.related_by_senders:
        clauses.append(
            "(%s)"
            % " OR ".join(
                "related_event.sender = ?" for _ in event_filter.related_by_senders
            )
        )
        args.extend(event_filter.related_by_senders)

    if event_filter.related_by_rel_types:
        clauses.append(
            "(%s)"
            % " OR ".join(
                "relation_type = ?" for _ in event_filter.related_by_rel_types
            )
        )
        args.extend(event_filter.related_by_rel_types)

    if event_filter.rel_types:
        clauses.append(
            "(%s)"
            % " OR ".join(
                "event_relation.relation_type = ?" for _ in event_filter.rel_types
            )
        )
        args.extend(event_filter.rel_types)

    if event_filter.not_rel_types:
        clauses.append(
            "((%s) OR event_relation.relation_type IS NULL)"
            % " AND ".join(
                "event_relation.relation_type != ?" for _ in event_filter.not_rel_types
            )
        )
        args.extend(event_filter.not_rel_types)

    return " AND ".join(clauses), args


class StreamWorkerStore(EventsWorkerStore, SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._instance_name = hs.get_instance_name()
        self._send_federation = hs.should_send_federation()
        self._federation_shard_config = hs.config.worker.federation_shard_config

        # If we're a process that sends federation we may need to reset the
        # `federation_stream_position` table to match the current sharding
        # config. We don't do this now as otherwise two processes could conflict
        # during startup which would cause one to die.
        self._need_to_reset_federation_stream_positions = self._send_federation

        events_max = self.get_room_max_stream_ordering()
        event_cache_prefill, min_event_val = self.db_pool.get_cache_dict(
            db_conn,
            "events",
            entity_column="room_id",
            stream_column="stream_ordering",
            max_value=events_max,
        )
        self._events_stream_cache = StreamChangeCache(
            "EventsRoomStreamChangeCache",
            min_event_val,
            prefilled_cache=event_cache_prefill,
        )
        self._membership_stream_cache = StreamChangeCache(
            "MembershipStreamChangeCache", events_max
        )

        self._stream_order_on_start = self.get_room_max_stream_ordering()
        self._min_stream_order_on_start = self.get_room_min_stream_ordering()

    def get_room_max_stream_ordering(self) -> int:
        """Get the stream_ordering of regular events that we have committed up to

        Returns the maximum stream id such that all stream ids less than or
        equal to it have been successfully persisted.
        """
        return self._stream_id_gen.get_current_token()

    def get_room_min_stream_ordering(self) -> int:
        """Get the stream_ordering of backfilled events that we have committed up to

        Backfilled events use *negative* stream orderings, so this returns the
        minimum negative stream id such that all stream ids greater than or
        equal to it have been successfully persisted.
        """
        return self._backfill_id_gen.get_current_token()

    def get_room_max_token(self) -> RoomStreamToken:
        """Get a `RoomStreamToken` that marks the current maximum persisted
        position of the events stream. Useful to get a token that represents
        "now".

        The token returned is a "live" token that may have an instance_map
        component.
        """

        min_pos = self._stream_id_gen.get_current_token()

        positions = {}
        if isinstance(self._stream_id_gen, MultiWriterIdGenerator):
            # The `min_pos` is the minimum position that we know all instances
            # have finished persisting to, so we only care about instances whose
            # positions are ahead of that. (Instance positions can be behind the
            # min position as there are times we can work out that the minimum
            # position is ahead of the naive minimum across all current
            # positions. See MultiWriterIdGenerator for details)
            positions = {
                i: p
                for i, p in self._stream_id_gen.get_positions().items()
                if p > min_pos
            }

        return RoomStreamToken(stream=min_pos, instance_map=immutabledict(positions))

    def get_events_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._stream_id_gen

    async def get_room_events_stream_for_rooms(
        self,
        *,
        room_ids: Collection[str],
        from_key: RoomStreamToken,
        to_key: Optional[RoomStreamToken] = None,
        direction: Direction = Direction.BACKWARDS,
        limit: int = 0,
    ) -> Dict[str, Tuple[List[EventBase], RoomStreamToken, bool]]:
        """Get new room events in stream ordering since `from_key`.

        Args:
            room_ids
            from_key: The token to stream from (starting point and heading in the given
                direction)
            to_key: The token representing the end stream position (end point)
            limit: Maximum number of events to return
            direction: Indicates whether we are paginating forwards or backwards
                from `from_key`.

        Returns:
            A map from room id to a tuple containing:
                - list of recent events in the room
                - stream ordering key for the start of the chunk of events returned.
                - a boolean to indicate if there were more events but we hit the limit

            When Direction.FORWARDS: from_key < x <= to_key, (ascending order)
            When Direction.BACKWARDS: from_key >= x > to_key, (descending order)
        """
        if direction == Direction.FORWARDS:
            room_ids = self._events_stream_cache.get_entities_changed(
                room_ids, from_key.stream
            )
        elif direction == Direction.BACKWARDS:
            if to_key is not None:
                room_ids = self._events_stream_cache.get_entities_changed(
                    room_ids, to_key.stream
                )
        else:
            assert_never(direction)

        if not room_ids:
            return {}

        results = {}
        room_ids = list(room_ids)
        for rm_ids in (room_ids[i : i + 20] for i in range(0, len(room_ids), 20)):
            res = await make_deferred_yieldable(
                defer.gatherResults(
                    [
                        run_in_background(
                            self.paginate_room_events_by_stream_ordering,
                            room_id=room_id,
                            from_key=from_key,
                            to_key=to_key,
                            direction=direction,
                            limit=limit,
                        )
                        for room_id in rm_ids
                    ],
                    consumeErrors=True,
                )
            )
            results.update(dict(zip(rm_ids, res)))

        return results

    def get_rooms_that_changed(
        self, room_ids: Collection[str], from_key: RoomStreamToken
    ) -> Set[str]:
        """Given a list of rooms and a token, return rooms where there may have
        been changes.
        """
        from_id = from_key.stream
        return {
            room_id
            for room_id in room_ids
            if self._events_stream_cache.has_entity_changed(room_id, from_id)
        }

    async def get_rooms_that_have_updates_since_sliding_sync_table(
        self,
        room_ids: StrCollection,
        from_key: RoomStreamToken,
    ) -> StrCollection:
        """Return the rooms that probably have had updates since the given
        token (changes that are > `from_key`)."""
        # If the stream change cache is valid for the stream token, we can just
        # use the result of that.
        if from_key.stream >= self._events_stream_cache.get_earliest_known_position():
            return self._events_stream_cache.get_entities_changed(
                room_ids, from_key.stream
            )

        def get_rooms_that_have_updates_since_sliding_sync_table_txn(
            txn: LoggingTransaction,
        ) -> StrCollection:
            sql = """
                SELECT room_id
                FROM sliding_sync_joined_rooms
                WHERE {clause}
                    AND event_stream_ordering > ?
            """

            results: Set[str] = set()
            for batch in batch_iter(room_ids, 1000):
                clause, args = make_in_list_sql_clause(
                    self.database_engine, "room_id", batch
                )

                args.append(from_key.stream)
                txn.execute(sql.format(clause=clause), args)

                results.update(row[0] for row in txn)

            return results

        return await self.db_pool.runInteraction(
            "get_rooms_that_have_updates_since_sliding_sync_table",
            get_rooms_that_have_updates_since_sliding_sync_table_txn,
        )

    async def paginate_room_events_by_stream_ordering(
        self,
        *,
        room_id: str,
        from_key: RoomStreamToken,
        to_key: Optional[RoomStreamToken] = None,
        direction: Direction = Direction.BACKWARDS,
        limit: int = 0,
    ) -> Tuple[List[EventBase], RoomStreamToken, bool]:
        """
        Paginate events by `stream_ordering` in the room from the `from_key` in the
        given `direction` to the `to_key` or `limit`.

        Args:
            room_id
            from_key: The token to stream from (starting point and heading in the given
                direction)
            to_key: The token representing the end stream position (end point)
            direction: Indicates whether we are paginating forwards or backwards
                from `from_key`.
            limit: Maximum number of events to return

        Returns:
            The results as a list of events, a token that points to the end of
            the result set, and a boolean to indicate if there were more events
            but we hit the limit. If no events are returned then the end of the
            stream has been reached (i.e. there are no events between `from_key`
            and `to_key`).

            When Direction.FORWARDS: from_key < x <= to_key, (ascending order)
            When Direction.BACKWARDS: from_key >= x > to_key, (descending order)
        """

        # FIXME: When going forwards, we should enforce that the `to_key` is not `None`
        # because we always need an upper bound when querying the events stream (as
        # otherwise we'll potentially pick up events that are not fully persisted).

        # We should only be working with `stream_ordering` tokens here
        assert from_key is None or from_key.topological is None
        assert to_key is None or to_key.topological is None

        # We can bail early if we're looking forwards, and our `to_key` is already
        # before our `from_key`.
        if (
            direction == Direction.FORWARDS
            and to_key is not None
            and to_key.is_before_or_eq(from_key)
        ):
            # Token selection matches what we do below if there are no rows
            return [], to_key if to_key else from_key, False
        # Or vice-versa, if we're looking backwards and our `from_key` is already before
        # our `to_key`.
        elif (
            direction == Direction.BACKWARDS
            and to_key is not None
            and from_key.is_before_or_eq(to_key)
        ):
            # Token selection matches what we do below if there are no rows
            return [], to_key if to_key else from_key, False

        # We can do a quick sanity check to see if any events have been sent in the room
        # since the earlier token.
        has_changed = True
        if direction == Direction.FORWARDS:
            has_changed = self._events_stream_cache.has_entity_changed(
                room_id, from_key.stream
            )
        elif direction == Direction.BACKWARDS:
            if to_key is not None:
                has_changed = self._events_stream_cache.has_entity_changed(
                    room_id, to_key.stream
                )
        else:
            assert_never(direction)

        if not has_changed:
            # Token selection matches what we do below if there are no rows
            return [], to_key if to_key else from_key, False

        order, from_bound, to_bound = generate_pagination_bounds(
            direction, from_key, to_key
        )

        bounds = generate_pagination_where_clause(
            direction=direction,
            # The empty string will shortcut downstream code to only use the
            # `stream_ordering` column
            column_names=("", "stream_ordering"),
            from_token=from_bound,
            to_token=to_bound,
            engine=self.database_engine,
        )

        def f(txn: LoggingTransaction) -> Tuple[List[_EventDictReturn], bool]:
            sql = f"""
                SELECT event_id, instance_name, stream_ordering
                FROM events
                WHERE
                    room_id = ?
                    AND not outlier
                    AND {bounds}
                ORDER BY stream_ordering {order} LIMIT ?
            """
            txn.execute(sql, (room_id, 2 * limit))

            # Get all the rows and check if we hit the limit.
            fetched_rows = txn.fetchall()
            limited = len(fetched_rows) >= 2 * limit

            rows = [
                _EventDictReturn(event_id, None, stream_ordering)
                for event_id, instance_name, stream_ordering in fetched_rows
                if _filter_results_by_stream(
                    lower_token=(
                        to_key if direction == Direction.BACKWARDS else from_key
                    ),
                    upper_token=(
                        from_key if direction == Direction.BACKWARDS else to_key
                    ),
                    instance_name=instance_name,
                    stream_ordering=stream_ordering,
                )
            ]

            if len(rows) > limit:
                limited = True

            rows = rows[:limit]
            return rows, limited

        rows, limited = await self.db_pool.runInteraction(
            "get_room_events_stream_for_room", f
        )

        ret = await self.get_events_as_list(
            [r.event_id for r in rows], get_prev_content=True
        )

        if rows:
            next_key = generate_next_token(
                direction=direction,
                last_topo_ordering=None,
                last_stream_ordering=rows[-1].stream_ordering,
            )
        else:
            # TODO (erikj): We should work out what to do here instead. (same as
            # `_paginate_room_events_by_topological_ordering_txn(...)`)
            next_key = to_key if to_key else from_key

        return ret, next_key, limited

    @trace
    async def get_current_state_delta_membership_changes_for_user(
        self,
        user_id: str,
        from_key: RoomStreamToken,
        to_key: RoomStreamToken,
        excluded_room_ids: Optional[List[str]] = None,
    ) -> List[CurrentStateDeltaMembership]:
        """
        Fetch membership events (and the previous event that was replaced by that one)
        for a given user.

        Note: This function only works with "live" tokens with `stream_ordering` only.

        We're looking for membership changes in the token range (> `from_key` and <=
        `to_key`).

        Please be mindful to only use this with `from_key` and `to_key` tokens that are
        recent enough to be after when the first local user joined the room. Otherwise,
        the results may be incomplete or too greedy. For example, if you use a token
        range before the first local user joined the room, you will see 0 events since
        `current_state_delta_stream` tracks what the server thinks is the current state
        of the room as time goes. It does not track how state progresses from the
        beginning of the room. So for example, when you remotely join a room, the first
        rows will just be the state when you joined and progress from there.

        You can probably reasonably use this with `/sync` because the `to_key` passed in
        will be the "current" now token and the range will cover when the user joined
        the room.

        Args:
            user_id: The user ID to fetch membership events for.
            from_key: The point in the stream to sync from (fetching events > this point).
            to_key: The token to fetch rooms up to (fetching events <= this point).
            excluded_room_ids: Optional list of room IDs to exclude from the results.

        Returns:
            All membership changes to the current state in the token range. Events are
            sorted by `stream_ordering` ascending.

            `event_id`/`sender` can be `None` when the server leaves a room (meaning
            everyone locally left) or a state reset which removed the person from the
            room. We can't tell the difference between the two cases with what's
            available in the `current_state_delta_stream` table. To actually check for a
            state reset, you need to check if a membership still exists in the room.
        """
        # Start by ruling out cases where a DB query is not necessary.
        if from_key == to_key:
            return []

        if from_key:
            has_changed = self._membership_stream_cache.has_entity_changed(
                user_id, int(from_key.stream)
            )
            if not has_changed:
                return []

        def f(txn: LoggingTransaction) -> List[CurrentStateDeltaMembership]:
            # To handle tokens with a non-empty instance_map we fetch more
            # results than necessary and then filter down
            min_from_id = from_key.stream
            max_to_id = to_key.get_max_stream_pos()

            args: List[Any] = [min_from_id, max_to_id, EventTypes.Member, user_id]

            # TODO: It would be good to assert that the `from_token`/`to_token` is >=
            # the first row in `current_state_delta_stream` for the rooms we're
            # interested in. Otherwise, we will end up with empty results and not know
            # it.

            # We could `COALESCE(e.stream_ordering, s.stream_id)` to get more accurate
            # stream positioning when available but given our usages, we can avoid the
            # complexity. Between two (valid) stream tokens, we will still get all of
            # the state changes. Since those events are persisted in a batch, valid
            # tokens will either be before or after the batch of events.
            #
            # `stream_ordering` from the `events` table is more accurate when available
            # since the `current_state_delta_stream` table only tracks that the current
            # state is at this stream position (not what stream position the state event
            # was added) and uses the *minimum* stream position for batches of events.
            sql = """
                SELECT
                    s.room_id,
                    e.event_id,
                    s.instance_name,
                    s.stream_id,
                    m.membership,
                    e.sender,
                    s.prev_event_id,
                    e_prev.instance_name AS prev_instance_name,
                    e_prev.stream_ordering AS prev_stream_ordering,
                    m_prev.membership AS prev_membership,
                    e_prev.sender AS prev_sender
                FROM current_state_delta_stream AS s
                    LEFT JOIN events AS e ON e.event_id = s.event_id
                    LEFT JOIN room_memberships AS m ON m.event_id = s.event_id
                    LEFT JOIN events AS e_prev ON e_prev.event_id = s.prev_event_id
                    LEFT JOIN room_memberships AS m_prev ON m_prev.event_id = s.prev_event_id
                WHERE s.stream_id > ? AND s.stream_id <= ?
                    AND s.type = ?
                    AND s.state_key = ?
                ORDER BY s.stream_id ASC
            """

            txn.execute(sql, args)

            membership_changes: List[CurrentStateDeltaMembership] = []
            for (
                room_id,
                event_id,
                instance_name,
                stream_ordering,
                membership,
                sender,
                prev_event_id,
                prev_instance_name,
                prev_stream_ordering,
                prev_membership,
                prev_sender,
            ) in txn:
                assert room_id is not None
                assert stream_ordering is not None

                if _filter_results_by_stream(
                    from_key,
                    to_key,
                    instance_name,
                    stream_ordering,
                ):
                    # When the server leaves a room, it will insert new rows into the
                    # `current_state_delta_stream` table with `event_id = null` for all
                    # current state. This means we might already have a row for the
                    # leave event and then another for the same leave where the
                    # `event_id=null` but the `prev_event_id` is pointing back at the
                    # earlier leave event. We don't want to report the leave, if we
                    # already have a leave event.
                    if event_id is None and prev_membership == Membership.LEAVE:
                        continue

                    membership_change = CurrentStateDeltaMembership(
                        room_id=room_id,
                        # Event
                        event_id=event_id,
                        event_pos=PersistedEventPosition(
                            # If instance_name is null we default to "master"
                            instance_name=instance_name or "master",
                            stream=stream_ordering,
                        ),
                        # When `s.event_id = null`, we won't be able to get respective
                        # `room_membership` but can assume the user has left the room
                        # because this only happens when the server leaves a room
                        # (meaning everyone locally left) or a state reset which removed
                        # the person from the room.
                        membership=(
                            membership if membership is not None else Membership.LEAVE
                        ),
                        # This will also be null for the same reasons if `s.event_id = null`
                        sender=sender,
                        # Prev event
                        prev_event_id=prev_event_id,
                        prev_event_pos=(
                            PersistedEventPosition(
                                # If instance_name is null we default to "master"
                                instance_name=prev_instance_name or "master",
                                stream=prev_stream_ordering,
                            )
                            if (prev_stream_ordering is not None)
                            else None
                        ),
                        prev_membership=prev_membership,
                        prev_sender=prev_sender,
                    )

                    membership_changes.append(membership_change)

            return membership_changes

        membership_changes = await self.db_pool.runInteraction(
            "get_current_state_delta_membership_changes_for_user", f
        )

        room_ids_to_exclude: AbstractSet[str] = set()
        if excluded_room_ids is not None:
            room_ids_to_exclude = set(excluded_room_ids)

        return [
            membership_change
            for membership_change in membership_changes
            if membership_change.room_id not in room_ids_to_exclude
        ]

    @cancellable
    async def get_membership_changes_for_user(
        self,
        user_id: str,
        from_key: RoomStreamToken,
        to_key: RoomStreamToken,
        excluded_rooms: Optional[List[str]] = None,
    ) -> List[EventBase]:
        """Fetch membership events for a given user.

        All such events whose stream ordering `s` lies in the range
        `from_key < s <= to_key` are returned. Events are ordered by ascending stream
        order.
        """
        # Start by ruling out cases where a DB query is not necessary.
        if from_key == to_key:
            return []

        if from_key:
            has_changed = self._membership_stream_cache.has_entity_changed(
                user_id, int(from_key.stream)
            )
            if not has_changed:
                return []

        def f(txn: LoggingTransaction) -> List[_EventDictReturn]:
            # To handle tokens with a non-empty instance_map we fetch more
            # results than necessary and then filter down
            min_from_id = from_key.stream
            max_to_id = to_key.get_max_stream_pos()

            args: List[Any] = [user_id, min_from_id, max_to_id]

            ignore_room_clause = ""
            if excluded_rooms is not None and len(excluded_rooms) > 0:
                ignore_room_clause, ignore_room_args = make_in_list_sql_clause(
                    txn.database_engine, "e.room_id", excluded_rooms, negative=True
                )
                ignore_room_clause = f"AND {ignore_room_clause}"
                args += ignore_room_args

            sql = """
                SELECT m.event_id, instance_name, topological_ordering, stream_ordering
                FROM events AS e, room_memberships AS m
                WHERE e.event_id = m.event_id
                    AND m.user_id = ?
                    AND e.stream_ordering > ? AND e.stream_ordering <= ?
                    %s
                ORDER BY e.stream_ordering ASC
            """ % (ignore_room_clause,)

            txn.execute(sql, args)

            rows = [
                _EventDictReturn(event_id, None, stream_ordering)
                for event_id, instance_name, topological_ordering, stream_ordering in txn
                if _filter_results(
                    from_key,
                    to_key,
                    instance_name,
                    topological_ordering,
                    stream_ordering,
                )
            ]

            return rows

        rows = await self.db_pool.runInteraction("get_membership_changes_for_user", f)

        ret = await self.get_events_as_list(
            [r.event_id for r in rows], get_prev_content=True
        )

        return ret

    async def get_recent_events_for_room(
        self, room_id: str, limit: int, end_token: RoomStreamToken
    ) -> Tuple[List[EventBase], RoomStreamToken]:
        """Get the most recent events in the room in topological ordering.

        Args:
            room_id
            limit
            end_token: The stream token representing now.

        Returns:
            A list of events and a token pointing to the start of the returned
            events. The events returned are in ascending topological order.
        """

        rows, token = await self.get_recent_event_ids_for_room(
            room_id, limit, end_token
        )

        events = await self.get_events_as_list(
            [r.event_id for r in rows], get_prev_content=True
        )

        return events, token

    async def get_recent_event_ids_for_room(
        self, room_id: str, limit: int, end_token: RoomStreamToken
    ) -> Tuple[List[_EventDictReturn], RoomStreamToken]:
        """Get the most recent events in the room in topological ordering.

        Args:
            room_id
            limit
            end_token: The stream token representing now.

        Returns:
            A list of _EventDictReturn and a token pointing to the start of the
            returned events. The events returned are in ascending order.
        """
        # Allow a zero limit here, and no-op.
        if limit == 0:
            return [], end_token

        rows, token, _ = await self.db_pool.runInteraction(
            "get_recent_event_ids_for_room",
            self._paginate_room_events_by_topological_ordering_txn,
            room_id,
            from_token=end_token,
            limit=limit,
        )

        # We want to return the results in ascending order.
        rows.reverse()

        return rows, token

    async def get_room_event_before_stream_ordering(
        self, room_id: str, stream_ordering: int
    ) -> Optional[Tuple[int, int, str]]:
        """Gets details of the first event in a room at or before a stream ordering

        Args:
            room_id:
            stream_ordering:

        Returns:
            A tuple of (stream ordering, topological ordering, event_id)
        """

        def _f(txn: LoggingTransaction) -> Optional[Tuple[int, int, str]]:
            sql = """
                SELECT stream_ordering, topological_ordering, event_id
                FROM events
                LEFT JOIN rejections USING (event_id)
                WHERE room_id = ?
                    AND stream_ordering <= ?
                    AND NOT outlier
                    AND rejections.event_id IS NULL
                ORDER BY stream_ordering DESC
                LIMIT 1
            """
            txn.execute(sql, (room_id, stream_ordering))
            return cast(Optional[Tuple[int, int, str]], txn.fetchone())

        return await self.db_pool.runInteraction(
            "get_room_event_before_stream_ordering", _f
        )

    async def get_last_event_id_in_room_before_stream_ordering(
        self,
        room_id: str,
        end_token: RoomStreamToken,
    ) -> Optional[str]:
        """Returns the ID of the last event in a room at or before a stream ordering

        Args:
            room_id
            end_token: The token used to stream from

        Returns:
            The ID of the most recent event, or None if there are no events in the room
            before this stream ordering.
        """
        last_event_result = (
            await self.get_last_event_pos_in_room_before_stream_ordering(
                room_id, end_token
            )
        )

        if last_event_result:
            return last_event_result[0]

        return None

    async def get_last_event_pos_in_room(
        self,
        room_id: str,
        event_types: Optional[StrCollection] = None,
    ) -> Optional[Tuple[str, PersistedEventPosition]]:
        """
        Returns the ID and event position of the last event in a room.

        Based on `get_last_event_pos_in_room_before_stream_ordering(...)`

        Args:
            room_id
            event_types: Optional allowlist of event types to filter by

        Returns:
            The ID of the most recent event and it's position, or None if there are no
            events in the room that match the given event types.
        """

        def _get_last_event_pos_in_room_txn(
            txn: LoggingTransaction,
        ) -> Optional[Tuple[str, PersistedEventPosition]]:
            event_type_clause = ""
            event_type_args: List[str] = []
            if event_types is not None and len(event_types) > 0:
                event_type_clause, event_type_args = make_in_list_sql_clause(
                    txn.database_engine, "type", event_types
                )
                event_type_clause = f"AND {event_type_clause}"

            sql = f"""
            SELECT event_id, stream_ordering, instance_name
            FROM events
            LEFT JOIN rejections USING (event_id)
            WHERE room_id = ?
                {event_type_clause}
                AND NOT outlier
                AND rejections.event_id IS NULL
            ORDER BY stream_ordering DESC
            LIMIT 1
            """

            txn.execute(
                sql,
                [room_id] + event_type_args,
            )

            row = cast(Optional[Tuple[str, int, str]], txn.fetchone())
            if row is not None:
                event_id, stream_ordering, instance_name = row

                return event_id, PersistedEventPosition(
                    # If instance_name is null we default to "master"
                    instance_name or "master",
                    stream_ordering,
                )

            return None

        return await self.db_pool.runInteraction(
            "get_last_event_pos_in_room",
            _get_last_event_pos_in_room_txn,
        )

    @trace
    async def get_last_event_pos_in_room_before_stream_ordering(
        self,
        room_id: str,
        end_token: RoomStreamToken,
        event_types: Optional[StrCollection] = None,
    ) -> Optional[Tuple[str, PersistedEventPosition]]:
        """
        Returns the ID and event position of the last event in a room at or before a
        stream ordering.

        Args:
            room_id
            end_token: The token used to stream from
            event_types: Optional allowlist of event types to filter by

        Returns:
            The ID of the most recent event and it's position, or None if there are no
            events in the room before this stream ordering.
        """

        def get_last_event_pos_in_room_before_stream_ordering_txn(
            txn: LoggingTransaction,
        ) -> Optional[Tuple[str, PersistedEventPosition]]:
            # We're looking for the closest event at or before the token. We need to
            # handle the fact that the stream token can be a vector clock (with an
            # `instance_map`) and events can be persisted on different instances
            # (sharded event persisters). The first subquery handles the events that
            # would be within the vector clock and gets all rows between the minimum and
            # maximum stream ordering in the token which need to be filtered against the
            # `instance_map`. The second subquery handles the "before" case and finds
            # the first row before the token. We then filter out any results past the
            # token's vector clock and return the first row that matches.
            min_stream = end_token.stream
            max_stream = end_token.get_max_stream_pos()

            event_type_clause = ""
            event_type_args: List[str] = []
            if event_types is not None and len(event_types) > 0:
                event_type_clause, event_type_args = make_in_list_sql_clause(
                    txn.database_engine, "type", event_types
                )
                event_type_clause = f"AND {event_type_clause}"

            # We use `UNION ALL` because we don't need any of the deduplication logic
            # (`UNION` is really a `UNION` + `DISTINCT`). `UNION ALL` does preserve the
            # ordering of the operand queries but there is no actual guarantee that it
            # has this behavior in all scenarios so we need the extra `ORDER BY` at the
            # bottom.
            sql = """
                SELECT * FROM (
                    SELECT instance_name, stream_ordering, topological_ordering, event_id
                    FROM events
                    LEFT JOIN rejections USING (event_id)
                    WHERE room_id = ?
                        %s
                        AND ? < stream_ordering AND stream_ordering <= ?
                        AND NOT outlier
                        AND rejections.event_id IS NULL
                    ORDER BY stream_ordering DESC
                ) AS a
                UNION ALL
                SELECT * FROM (
                    SELECT instance_name, stream_ordering, topological_ordering, event_id
                    FROM events
                    LEFT JOIN rejections USING (event_id)
                    WHERE room_id = ?
                        %s
                        AND stream_ordering <= ?
                        AND NOT outlier
                        AND rejections.event_id IS NULL
                    ORDER BY stream_ordering DESC
                    LIMIT 1
                ) AS b
                ORDER BY stream_ordering DESC
            """ % (
                event_type_clause,
                event_type_clause,
            )
            txn.execute(
                sql,
                [room_id]
                + event_type_args
                + [min_stream, max_stream, room_id]
                + event_type_args
                + [min_stream],
            )

            for instance_name, stream_ordering, topological_ordering, event_id in txn:
                if _filter_results(
                    lower_token=None,
                    upper_token=end_token,
                    instance_name=instance_name,
                    topological_ordering=topological_ordering,
                    stream_ordering=stream_ordering,
                ):
                    return event_id, PersistedEventPosition(
                        # If instance_name is null we default to "master"
                        instance_name or "master",
                        stream_ordering,
                    )

            return None

        return await self.db_pool.runInteraction(
            "get_last_event_pos_in_room_before_stream_ordering",
            get_last_event_pos_in_room_before_stream_ordering_txn,
        )

    async def bulk_get_last_event_pos_in_room_before_stream_ordering(
        self,
        room_ids: StrCollection,
        end_token: RoomStreamToken,
    ) -> Dict[str, int]:
        """Bulk fetch the stream position of the latest events in the given
        rooms
        """

        # First we just get the latest positions for the room, as the vast
        # majority of them will be before the given end token anyway. By doing
        # this we can cache most rooms.
        uncapped_results = await self._bulk_get_max_event_pos(room_ids)

        # Check that the stream position for the rooms are from before the
        # minimum position of the token. If not then we need to fetch more
        # rows.
        results: Dict[str, int] = {}
        recheck_rooms: Set[str] = set()
        min_token = end_token.stream
        for room_id, stream in uncapped_results.items():
            if stream is None:
                # Despite the function not directly setting None, the cache can!
                # See: https://github.com/element-hq/synapse/issues/17726
                continue
            if stream <= min_token:
                results[room_id] = stream
            else:
                recheck_rooms.add(room_id)

        if not recheck_rooms:
            return results

        # There shouldn't be many rooms that we need to recheck, so we do them
        # one-by-one.
        for room_id in recheck_rooms:
            result = await self.get_last_event_pos_in_room_before_stream_ordering(
                room_id, end_token
            )
            if result is not None:
                results[room_id] = result[1].stream

        return results

    @cached()
    async def _get_max_event_pos(self, room_id: str) -> int:
        raise NotImplementedError()

    @cachedList(cached_method_name="_get_max_event_pos", list_name="room_ids")
    async def _bulk_get_max_event_pos(
        self, room_ids: StrCollection
    ) -> Mapping[str, Optional[int]]:
        """Fetch the max position of a persisted event in the room."""

        # We need to be careful not to return positions ahead of the current
        # positions, so we get the current token now and cap our queries to it.
        now_token = self.get_room_max_token()
        max_pos = now_token.get_max_stream_pos()

        results: Dict[str, int] = {}

        # First, we check for the rooms in the stream change cache to see if we
        # can just use the latest position from it.
        missing_room_ids: Set[str] = set()
        for room_id in room_ids:
            stream_pos = self._events_stream_cache.get_max_pos_of_last_change(room_id)
            if stream_pos is not None:
                results[room_id] = stream_pos
            else:
                missing_room_ids.add(room_id)

        if not missing_room_ids:
            return results

        # Next, we query the stream position from the DB. At first we fetch all
        # positions less than the *max* stream pos in the token, then filter
        # them down. We do this as a) this is a cheaper query, and b) the vast
        # majority of rooms will have a latest token from before the min stream
        # pos.

        def bulk_get_max_event_pos_fallback_txn(
            txn: LoggingTransaction, batched_room_ids: StrCollection
        ) -> Dict[str, int]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "room_id", batched_room_ids
            )
            sql = f"""
                SELECT room_id, (
                    SELECT stream_ordering FROM events AS e
                    LEFT JOIN rejections USING (event_id)
                    WHERE e.room_id = r.room_id
                        AND e.stream_ordering <= ?
                        AND NOT outlier
                        AND rejection_reason IS NULL
                    ORDER BY stream_ordering DESC
                    LIMIT 1
                )
                FROM rooms AS r
                WHERE {clause}
            """
            txn.execute(sql, [max_pos] + args)
            return {row[0]: row[1] for row in txn}

        # It's easier to look at the `sliding_sync_joined_rooms` table and avoid all of
        # the joins and sub-queries.
        def bulk_get_max_event_pos_from_sliding_sync_tables_txn(
            txn: LoggingTransaction, batched_room_ids: StrCollection
        ) -> Dict[str, int]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "room_id", batched_room_ids
            )
            sql = f"""
                SELECT room_id, event_stream_ordering
                FROM sliding_sync_joined_rooms
                WHERE {clause}
                ORDER BY event_stream_ordering DESC
            """
            txn.execute(sql, args)
            return {row[0]: row[1] for row in txn}

        recheck_rooms: Set[str] = set()
        for batched in batch_iter(room_ids, 1000):
            if await self.have_finished_sliding_sync_background_jobs():
                batch_results = await self.db_pool.runInteraction(
                    "bulk_get_max_event_pos_from_sliding_sync_tables_txn",
                    bulk_get_max_event_pos_from_sliding_sync_tables_txn,
                    batched,
                )
            else:
                batch_results = await self.db_pool.runInteraction(
                    "bulk_get_max_event_pos_fallback_txn",
                    bulk_get_max_event_pos_fallback_txn,
                    batched,
                )
            for room_id, stream_ordering in batch_results.items():
                if stream_ordering <= now_token.stream:
                    results[room_id] = stream_ordering
                else:
                    recheck_rooms.add(room_id)

        # We now need to handle rooms where the above query returned a stream
        # position that was potentially too new. This should happen very rarely
        # so we just query the rooms one-by-one
        for room_id in recheck_rooms:
            result = await self.get_last_event_pos_in_room_before_stream_ordering(
                room_id, now_token
            )
            if result is not None:
                results[room_id] = result[1].stream

        return results

    async def get_current_room_stream_token_for_room_id(
        self, room_id: str
    ) -> RoomStreamToken:
        """Returns the current position of the rooms stream (historic token)."""
        stream_ordering = self.get_room_max_stream_ordering()
        topo = await self.db_pool.runInteraction(
            "_get_max_topological_txn", self._get_max_topological_txn, room_id
        )
        return RoomStreamToken(topological=topo, stream=stream_ordering)

    @overload
    def get_stream_id_for_event_txn(
        self,
        txn: LoggingTransaction,
        event_id: str,
        allow_none: Literal[False] = False,
    ) -> int: ...

    @overload
    def get_stream_id_for_event_txn(
        self,
        txn: LoggingTransaction,
        event_id: str,
        allow_none: bool = False,
    ) -> Optional[int]: ...

    def get_stream_id_for_event_txn(
        self,
        txn: LoggingTransaction,
        event_id: str,
        allow_none: bool = False,
    ) -> Optional[int]:
        # Type ignore: we pass keyvalues a Dict[str, str]; the function wants
        # Dict[str, Any]. I think mypy is unhappy because Dict is invariant?
        return self.db_pool.simple_select_one_onecol_txn(  # type: ignore[call-overload]
            txn=txn,
            table="events",
            keyvalues={"event_id": event_id},
            retcol="stream_ordering",
            allow_none=allow_none,
        )

    async def get_position_for_event(self, event_id: str) -> PersistedEventPosition:
        """Get the persisted position for an event"""
        row = await self.db_pool.simple_select_one(
            table="events",
            keyvalues={"event_id": event_id},
            retcols=("stream_ordering", "instance_name"),
            desc="get_position_for_event",
        )

        return PersistedEventPosition(row[1] or "master", row[0])

    async def get_topological_token_for_event(self, event_id: str) -> RoomStreamToken:
        """The stream token for an event
        Args:
            event_id: The id of the event to look up a stream token for.
        Raises:
            StoreError if the event wasn't in the database.
        Returns:
            A `RoomStreamToken` topological token.
        """
        row = await self.db_pool.simple_select_one(
            table="events",
            keyvalues={"event_id": event_id},
            retcols=("stream_ordering", "topological_ordering"),
            desc="get_topological_token_for_event",
        )
        return RoomStreamToken(topological=row[1], stream=row[0])

    async def get_current_topological_token(self, room_id: str, stream_key: int) -> int:
        """Gets the topological token in a room after or at the given stream
        ordering.

        Args:
            room_id
            stream_key
        """
        if isinstance(self.database_engine, PostgresEngine):
            min_function = "LEAST"
        elif isinstance(self.database_engine, Sqlite3Engine):
            min_function = "MIN"
        else:
            raise RuntimeError(f"Unknown database engine {self.database_engine}")

        # This query used to be
        #    SELECT COALESCE(MIN(topological_ordering), 0) FROM events
        #    WHERE room_id = ? and events.stream_ordering >= {stream_key}
        # which returns 0 if the stream_key is newer than any event in
        # the room. That's not wrong, but it seems to interact oddly with backfill,
        # requiring a second call to /messages to actually backfill from a remote
        # homeserver.
        #
        # Instead, rollback the stream ordering to that after the most recent event in
        # this room.
        sql = f"""
            WITH fallback(max_stream_ordering) AS (
                SELECT MAX(stream_ordering)
                FROM events
                WHERE room_id = ?
            )
            SELECT COALESCE(MIN(topological_ordering), 0) FROM events
            WHERE
                room_id = ?
                AND events.stream_ordering >= {min_function}(
                    ?,
                    (SELECT max_stream_ordering FROM fallback)
                )
        """

        row = await self.db_pool.execute(
            "get_current_topological_token", sql, room_id, room_id, stream_key
        )
        return row[0][0] if row else 0

    def _get_max_topological_txn(self, txn: LoggingTransaction, room_id: str) -> int:
        txn.execute(
            "SELECT MAX(topological_ordering) FROM events WHERE room_id = ?",
            (room_id,),
        )

        rows = txn.fetchall()
        # An aggregate function like MAX() will always return one row per group
        # so we can safely rely on the lookup here. For example, when a we
        # lookup a `room_id` which does not exist, `rows` will look like
        # `[(None,)]`
        return rows[0][0] if rows[0][0] is not None else 0

    async def get_events_around(
        self,
        room_id: str,
        event_id: str,
        before_limit: int,
        after_limit: int,
        event_filter: Optional[Filter] = None,
    ) -> _EventsAround:
        """Retrieve events and pagination tokens around a given event in a
        room.
        """

        results = await self.db_pool.runInteraction(
            "get_events_around",
            self._get_events_around_txn,
            room_id,
            event_id,
            before_limit,
            after_limit,
            event_filter,
        )

        events_before = await self.get_events_as_list(
            list(results["before"]["event_ids"]), get_prev_content=True
        )

        events_after = await self.get_events_as_list(
            list(results["after"]["event_ids"]), get_prev_content=True
        )

        return _EventsAround(
            events_before=events_before,
            events_after=events_after,
            start=results["before"]["token"],
            end=results["after"]["token"],
        )

    def _get_events_around_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        event_id: str,
        before_limit: int,
        after_limit: int,
        event_filter: Optional[Filter],
    ) -> dict:
        """Retrieves event_ids and pagination tokens around a given event in a
        room.

        Args:
            room_id
            event_id
            before_limit
            after_limit
            event_filter

        Returns:
            dict
        """

        stream_ordering, topological_ordering = cast(
            Tuple[int, int],
            self.db_pool.simple_select_one_txn(
                txn,
                "events",
                keyvalues={"event_id": event_id, "room_id": room_id},
                retcols=["stream_ordering", "topological_ordering"],
            ),
        )

        # Paginating backwards includes the event at the token, but paginating
        # forward doesn't.
        before_token = RoomStreamToken(
            topological=topological_ordering - 1, stream=stream_ordering
        )

        after_token = RoomStreamToken(
            topological=topological_ordering, stream=stream_ordering
        )

        rows, start_token, _ = self._paginate_room_events_by_topological_ordering_txn(
            txn,
            room_id,
            before_token,
            direction=Direction.BACKWARDS,
            limit=before_limit,
            event_filter=event_filter,
        )
        events_before = [r.event_id for r in rows]

        rows, end_token, _ = self._paginate_room_events_by_topological_ordering_txn(
            txn,
            room_id,
            after_token,
            direction=Direction.FORWARDS,
            limit=after_limit,
            event_filter=event_filter,
        )
        events_after = [r.event_id for r in rows]

        return {
            "before": {"event_ids": events_before, "token": start_token},
            "after": {"event_ids": events_after, "token": end_token},
        }

    async def get_all_new_event_ids_stream(
        self,
        from_id: int,
        current_id: int,
        limit: int,
    ) -> Tuple[int, Dict[str, Optional[int]]]:
        """Get all new events

        Returns all event ids with from_id < stream_ordering <= current_id.

        Args:
            from_id:  the stream_ordering of the last event we processed
            current_id:  the stream_ordering of the most recently processed event
            limit: the maximum number of events to return

        Returns:
            A tuple of (next_id, event_to_received_ts), where `next_id`
            is the next value to pass as `from_id` (it will either be the
            stream_ordering of the last returned event, or, if fewer than `limit`
            events were found, the `current_id`). The `event_to_received_ts` is
            a dictionary mapping event ID to the event `received_ts`, sorted by ascending
            stream_ordering.
        """

        def get_all_new_event_ids_stream_txn(
            txn: LoggingTransaction,
        ) -> Tuple[int, Dict[str, Optional[int]]]:
            sql = (
                "SELECT e.stream_ordering, e.event_id, e.received_ts"
                " FROM events AS e"
                " WHERE"
                " ? < e.stream_ordering AND e.stream_ordering <= ?"
                " ORDER BY e.stream_ordering ASC"
                " LIMIT ?"
            )

            txn.execute(sql, (from_id, current_id, limit))
            rows = txn.fetchall()

            upper_bound = current_id
            if len(rows) == limit:
                upper_bound = rows[-1][0]

            event_to_received_ts: Dict[str, Optional[int]] = {
                row[1]: row[2] for row in rows
            }
            return upper_bound, event_to_received_ts

        upper_bound, event_to_received_ts = await self.db_pool.runInteraction(
            "get_all_new_event_ids_stream", get_all_new_event_ids_stream_txn
        )

        return upper_bound, event_to_received_ts

    async def get_federation_out_pos(self, typ: str) -> int:
        if self._need_to_reset_federation_stream_positions:
            await self.db_pool.runInteraction(
                "_reset_federation_positions_txn", self._reset_federation_positions_txn
            )
            self._need_to_reset_federation_stream_positions = False

        return await self.db_pool.simple_select_one_onecol(
            table="federation_stream_position",
            retcol="stream_id",
            keyvalues={"type": typ, "instance_name": self._instance_name},
            desc="get_federation_out_pos",
        )

    async def update_federation_out_pos(self, typ: str, stream_id: int) -> None:
        if self._need_to_reset_federation_stream_positions:
            await self.db_pool.runInteraction(
                "_reset_federation_positions_txn", self._reset_federation_positions_txn
            )
            self._need_to_reset_federation_stream_positions = False

        await self.db_pool.simple_update_one(
            table="federation_stream_position",
            keyvalues={"type": typ, "instance_name": self._instance_name},
            updatevalues={"stream_id": stream_id},
            desc="update_federation_out_pos",
        )

    def _reset_federation_positions_txn(self, txn: LoggingTransaction) -> None:
        """Fiddles with the `federation_stream_position` table to make it match
        the configured federation sender instances during start up.
        """

        # The federation sender instances may have changed, so we need to
        # massage the `federation_stream_position` table to have a row per type
        # per instance sending federation. If there is a mismatch we update the
        # table with the correct rows using the *minimum* stream ID seen. This
        # may result in resending of events/EDUs to remote servers, but that is
        # preferable to dropping them.

        if not self._send_federation:
            return

        # Pull out the configured instances. If we don't have a shard config then
        # we assume that we're the only instance sending.
        configured_instances = self._federation_shard_config.instances
        if not configured_instances:
            configured_instances = [self._instance_name]
        elif self._instance_name not in configured_instances:
            return

        instances_in_table = self.db_pool.simple_select_onecol_txn(
            txn,
            table="federation_stream_position",
            keyvalues={},
            retcol="instance_name",
        )

        if set(instances_in_table) == set(configured_instances):
            # Nothing to do
            return

        sql = """
            SELECT type, MIN(stream_id) FROM federation_stream_position
            GROUP BY type
        """
        txn.execute(sql)
        min_positions = dict(
            cast(Iterable[Tuple[str, int]], txn)
        )  # Map from type -> min position

        # Ensure we do actually have some values here
        assert set(min_positions) == {"federation", "events"}

        sql = """
            DELETE FROM federation_stream_position
            WHERE NOT (%s)
        """
        clause, args = make_in_list_sql_clause(
            txn.database_engine, "instance_name", configured_instances
        )
        txn.execute(sql % (clause,), args)

        for typ, stream_id in min_positions.items():
            self.db_pool.simple_upsert_txn(
                txn,
                table="federation_stream_position",
                keyvalues={"type": typ, "instance_name": self._instance_name},
                values={"stream_id": stream_id},
            )

    def has_room_changed_since(self, room_id: str, stream_id: int) -> bool:
        return self._events_stream_cache.has_entity_changed(room_id, stream_id)

    def _paginate_room_events_by_topological_ordering_txn(
        self,
        txn: LoggingTransaction,
        room_id: str,
        from_token: RoomStreamToken,
        to_token: Optional[RoomStreamToken] = None,
        direction: Direction = Direction.BACKWARDS,
        limit: int = 0,
        event_filter: Optional[Filter] = None,
    ) -> Tuple[List[_EventDictReturn], RoomStreamToken, bool]:
        """Returns list of events before or after a given token.

        Args:
            txn
            room_id
            from_token: The token used to stream from
            to_token: A token which if given limits the results to only those before
            direction: Indicates whether we are paginating forwards or backwards
                from `from_key`.
            limit: The maximum number of events to return.
            event_filter: If provided filters the events to
                those that match the filter.

        Returns:
            A list of _EventDictReturn, a token that points to the end of the
            result set, and a boolean to indicate if there were more events but
            we hit the limit.  If no events are returned then the end of the
            stream has been reached (i.e. there are no events between
            `from_token` and `to_token`), or `limit` is zero.
        """
        # We can bail early if we're looking forwards, and our `to_key` is already
        # before our `from_token`.
        if (
            direction == Direction.FORWARDS
            and to_token is not None
            and to_token.is_before_or_eq(from_token)
        ):
            # Token selection matches what we do below if there are no rows
            return [], to_token if to_token else from_token, False
        # Or vice-versa, if we're looking backwards and our `from_token` is already before
        # our `to_token`.
        elif (
            direction == Direction.BACKWARDS
            and to_token is not None
            and from_token.is_before_or_eq(to_token)
        ):
            # Token selection matches what we do below if there are no rows
            return [], to_token if to_token else from_token, False

        args: List[Any] = [room_id]

        order, from_bound, to_bound = generate_pagination_bounds(
            direction, from_token, to_token
        )

        bounds = generate_pagination_where_clause(
            direction=direction,
            column_names=("event.topological_ordering", "event.stream_ordering"),
            from_token=from_bound,
            to_token=to_bound,
            engine=self.database_engine,
        )

        filter_clause, filter_args = filter_to_clause(event_filter)

        if filter_clause:
            bounds += " AND " + filter_clause
            args.extend(filter_args)

        # We fetch more events as we'll filter the result set
        requested_limit = int(limit) * 2
        args.append(int(limit) * 2)

        select_keywords = "SELECT"
        join_clause = ""
        # Using DISTINCT in this SELECT query is quite expensive, because it
        # requires the engine to sort on the entire (not limited) result set,
        # i.e. the entire events table. Only use it in scenarios that could result
        # in the same event ID occurring multiple times in the results.
        needs_distinct = False
        if event_filter and event_filter.labels:
            # If we're not filtering on a label, then joining on event_labels will
            # return as many row for a single event as the number of labels it has. To
            # avoid this, only join if we're filtering on at least one label.
            join_clause += """
                LEFT JOIN event_labels
                USING (event_id, room_id, topological_ordering)
            """
            if len(event_filter.labels) > 1:
                # Multiple labels could cause the same event to appear multiple times.
                needs_distinct = True

        # If there is a relation_senders and relation_types filter join to the
        # relations table to get events related to the current event.
        if event_filter and (
            event_filter.related_by_senders or event_filter.related_by_rel_types
        ):
            # Filtering by relations could cause the same event to appear multiple
            # times (since there's no limit on the number of relations to an event).
            needs_distinct = True
            join_clause += """
                LEFT JOIN event_relations AS relation ON (event.event_id = relation.relates_to_id)
            """
            if event_filter.related_by_senders:
                join_clause += """
                    LEFT JOIN events AS related_event ON (relation.event_id = related_event.event_id)
                """

        # If there is a not_rel_types filter join to the relations table to get
        # the event's relation information.
        if event_filter and (event_filter.rel_types or event_filter.not_rel_types):
            join_clause += """
                LEFT JOIN event_relations AS event_relation USING (event_id)
            """

        if needs_distinct:
            select_keywords += " DISTINCT"

        sql = """
            %(select_keywords)s
                event.event_id, event.instance_name,
                event.topological_ordering, event.stream_ordering
            FROM events AS event
            %(join_clause)s
            WHERE event.outlier = FALSE AND event.room_id = ? AND %(bounds)s
            ORDER BY event.topological_ordering %(order)s,
            event.stream_ordering %(order)s LIMIT ?
        """ % {
            "select_keywords": select_keywords,
            "join_clause": join_clause,
            "bounds": bounds,
            "order": order,
        }
        txn.execute(sql, args)

        # Get all the rows and check if we hit the limit.
        fetched_rows = txn.fetchall()
        limited = len(fetched_rows) >= requested_limit

        # Filter the result set.
        rows = [
            _EventDictReturn(event_id, topological_ordering, stream_ordering)
            for event_id, instance_name, topological_ordering, stream_ordering in fetched_rows
            if _filter_results(
                lower_token=(
                    to_token if direction == Direction.BACKWARDS else from_token
                ),
                upper_token=(
                    from_token if direction == Direction.BACKWARDS else to_token
                ),
                instance_name=instance_name,
                topological_ordering=topological_ordering,
                stream_ordering=stream_ordering,
            )
        ]

        if len(rows) > limit:
            limited = True

        rows = rows[:limit]

        if rows:
            assert rows[-1].topological_ordering is not None
            next_token = generate_next_token(
                direction, rows[-1].topological_ordering, rows[-1].stream_ordering
            )
        else:
            # TODO (erikj): We should work out what to do here instead.
            next_token = to_token if to_token else from_token

        return rows, next_token, limited

    @trace
    @tag_args
    async def paginate_room_events_by_topological_ordering(
        self,
        *,
        room_id: str,
        from_key: RoomStreamToken,
        to_key: Optional[RoomStreamToken] = None,
        direction: Direction = Direction.BACKWARDS,
        limit: int = 0,
        event_filter: Optional[Filter] = None,
    ) -> Tuple[List[EventBase], RoomStreamToken, bool]:
        """
        Paginate events by `topological_ordering` (tie-break with `stream_ordering`) in
        the room from the `from_key` in the given `direction` to the `to_key` or
        `limit`.

        Args:
            room_id
            from_key: The token to stream from (starting point and heading in the given
                direction)
            to_key: The token representing the end stream position (end point)
            direction: Indicates whether we are paginating forwards or backwards
                from `from_key`.
            limit: Maximum number of events to return
            event_filter: If provided filters the events to those that match the filter.

        Returns:
            The results as a list of events, a token that points to the end of
            the result set, and a boolean to indicate if there were more events
            but we hit the limit. If no events are returned then the end of the
            stream has been reached (i.e. there are no events between `from_key`
            and `to_key`).

            When Direction.FORWARDS: from_key < x <= to_key, (ascending order)
            When Direction.BACKWARDS: from_key >= x > to_key, (descending order)
        """

        # FIXME: When going forwards, we should enforce that the `to_key` is not `None`
        # because we always need an upper bound when querying the events stream (as
        # otherwise we'll potentially pick up events that are not fully persisted).

        # We have these checks outside of the transaction function (txn) to save getting
        # a DB connection and switching threads if we don't need to.
        #
        # We can bail early if we're looking forwards, and our `to_key` is already
        # before our `from_key`.
        if (
            direction == Direction.FORWARDS
            and to_key is not None
            and to_key.is_before_or_eq(from_key)
        ):
            # Token selection matches what we do in `_paginate_room_events_txn` if there
            # are no rows
            return [], to_key if to_key else from_key, False
        # Or vice-versa, if we're looking backwards and our `from_key` is already before
        # our `to_key`.
        elif (
            direction == Direction.BACKWARDS
            and to_key is not None
            and from_key.is_before_or_eq(to_key)
        ):
            # Token selection matches what we do in `_paginate_room_events_txn` if there
            # are no rows
            return [], to_key if to_key else from_key, False

        rows, token, limited = await self.db_pool.runInteraction(
            "paginate_room_events_by_topological_ordering",
            self._paginate_room_events_by_topological_ordering_txn,
            room_id,
            from_key,
            to_key,
            direction,
            limit,
            event_filter,
        )

        events = await self.get_events_as_list(
            [r.event_id for r in rows], get_prev_content=True
        )

        return events, token, limited

    @cached()
    async def get_id_for_instance(self, instance_name: str) -> int:
        """Get a unique, immutable ID that corresponds to the given Synapse worker instance."""

        def _get_id_for_instance_txn(txn: LoggingTransaction) -> int:
            instance_id = self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="instance_map",
                keyvalues={"instance_name": instance_name},
                retcol="instance_id",
                allow_none=True,
            )
            if instance_id is not None:
                return instance_id

            # If we don't have an entry upsert one.
            #
            # We could do this before the first check, and rely on the cache for
            # efficiency, but each UPSERT causes the next ID to increment which
            # can quickly bloat the size of the generated IDs for new instances.
            self.db_pool.simple_upsert_txn(
                txn,
                table="instance_map",
                keyvalues={"instance_name": instance_name},
                values={},
            )

            return self.db_pool.simple_select_one_onecol_txn(
                txn,
                table="instance_map",
                keyvalues={"instance_name": instance_name},
                retcol="instance_id",
            )

        return await self.db_pool.runInteraction(
            "get_id_for_instance", _get_id_for_instance_txn
        )

    @cached()
    async def get_name_from_instance_id(self, instance_id: int) -> str:
        """Get the instance name from an ID previously returned by
        `get_id_for_instance`.
        """

        return await self.db_pool.simple_select_one_onecol(
            table="instance_map",
            keyvalues={"instance_id": instance_id},
            retcol="instance_name",
            desc="get_name_from_instance_id",
        )

    async def get_timeline_gaps(
        self,
        room_id: str,
        from_token: Optional[RoomStreamToken],
        to_token: RoomStreamToken,
    ) -> Optional[RoomStreamToken]:
        """Check if there is a gap, and return a token that marks the position
        of the gap in the stream.
        """

        sql = """
            SELECT instance_name, stream_ordering
            FROM timeline_gaps
            WHERE room_id = ? AND ? < stream_ordering AND stream_ordering <= ?
            ORDER BY stream_ordering
        """

        rows = await self.db_pool.execute(
            "get_timeline_gaps",
            sql,
            room_id,
            from_token.stream if from_token else 0,
            to_token.get_max_stream_pos(),
        )

        if not rows:
            return None

        positions = [
            PersistedEventPosition(instance_name, stream_ordering)
            for instance_name, stream_ordering in rows
        ]
        if from_token:
            positions = [p for p in positions if p.persisted_after(from_token)]

        positions = [p for p in positions if not p.persisted_after(to_token)]

        if positions:
            # We return a stream token that ensures the event *at* the position
            # of the gap is included (as the gap is *before* the persisted
            # event).
            last_position = positions[-1]
            return RoomStreamToken(stream=last_position.stream - 1)

        return None

    @trace
    def get_rooms_that_might_have_updates(
        self, room_ids: StrCollection, from_token: RoomStreamToken
    ) -> StrCollection:
        """Filters given room IDs down to those that might have updates, i.e.
        removes rooms that definitely do not have updates.
        """
        return self._events_stream_cache.get_entities_changed(
            room_ids, from_token.stream
        )
