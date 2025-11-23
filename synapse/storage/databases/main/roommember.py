#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import logging
from http import HTTPStatus
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Collection,
    Iterable,
    Mapping,
    Sequence,
    cast,
)

import attr

from synapse.api.constants import EventTypes, Membership
from synapse.api.errors import Codes, SynapseError
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.logging.opentracing import trace
from synapse.metrics import SERVER_NAME_LABEL, LaterGauge
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.databases.main.events_worker import EventsWorkerStore
from synapse.storage.databases.main.stream import _filter_results_by_stream
from synapse.storage.engines import Sqlite3Engine
from synapse.storage.roommember import (
    MemberSummary,
    ProfileInfo,
    RoomsForUser,
    RoomsForUserSlidingSync,
)
from synapse.types import (
    JsonDict,
    PersistedEventPosition,
    StateMap,
    StrCollection,
    StreamToken,
    get_domain_from_id,
)
from synapse.util.caches.descriptors import _CacheContext, cached, cachedList
from synapse.util.iterutils import batch_iter
from synapse.util.metrics import Measure

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


_MEMBERSHIP_PROFILE_UPDATE_NAME = "room_membership_profile_update"
_CURRENT_STATE_MEMBERSHIP_UPDATE_NAME = "current_state_events_membership"
_POPULATE_PARTICIPANT_BG_UPDATE_BATCH_SIZE = 1000


federation_known_servers_gauge = LaterGauge(
    name="synapse_federation_known_servers",
    desc="",
    labelnames=[SERVER_NAME_LABEL],
)


@attr.s(frozen=True, slots=True, auto_attribs=True)
class EventIdMembership:
    """Returned by `get_membership_from_event_ids`"""

    user_id: str
    membership: str


class RoomMemberWorkerStore(EventsWorkerStore, CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._server_notices_mxid = hs.config.servernotices.server_notices_mxid

        if (
            self.hs.config.worker.run_background_tasks
            and self.hs.config.metrics.metrics_flags.known_servers
        ):
            self._known_servers_count = 1
            self.hs.get_clock().looping_call(
                self._count_known_servers,
                60 * 1000,
            )
            self.hs.get_clock().call_later(
                1,
                self._count_known_servers,
            )
            federation_known_servers_gauge.register_hook(
                homeserver_instance_id=hs.get_instance_id(),
                hook=lambda: {(self.server_name,): self._known_servers_count},
            )

    @wrap_as_background_process("_count_known_servers")
    async def _count_known_servers(self) -> int:
        """
        Count the servers that this server knows about.

        The statistic is stored on the class for the
        `synapse_federation_known_servers` LaterGauge to collect.
        """

        def _transact(txn: LoggingTransaction) -> int:
            if isinstance(self.database_engine, Sqlite3Engine):
                query = """
                    SELECT COUNT(DISTINCT substr(out.user_id, pos+1))
                    FROM (
                        SELECT rm.user_id as user_id, instr(rm.user_id, ':')
                            AS pos FROM room_memberships as rm
                        INNER JOIN current_state_events as c ON rm.event_id = c.event_id
                        WHERE c.type = 'm.room.member'
                    ) as out
                """
            else:
                query = """
                    SELECT COUNT(DISTINCT split_part(state_key, ':', 2))
                    FROM current_state_events
                    WHERE type = 'm.room.member' AND membership = 'join';
                """
            txn.execute(query)
            return list(txn)[0][0]

        count = await self.db_pool.runInteraction("get_known_servers", _transact)

        # We always know about ourselves, even if we have nothing in
        # room_memberships (for example, the server is new).
        self._known_servers_count = max([count, 1])
        return self._known_servers_count

    @cached(max_entries=100000, iterable=True)
    async def get_users_in_room(self, room_id: str) -> Sequence[str]:
        """Returns a list of users in the room.

        Will return inaccurate results for rooms with partial state, since the state for
        the forward extremities of those rooms will exclude most members. We may also
        calculate room state incorrectly for such rooms and believe that a member is or
        is not in the room when the opposite is true.

        Note: If you only care about users in the room local to the homeserver, use
        `get_local_users_in_room(...)` instead which will be more performant.
        """
        return await self.db_pool.simple_select_onecol(
            table="current_state_events",
            keyvalues={
                "type": EventTypes.Member,
                "room_id": room_id,
                "membership": Membership.JOIN,
            },
            retcol="state_key",
            desc="get_users_in_room",
        )

    def get_users_in_room_txn(self, txn: LoggingTransaction, room_id: str) -> list[str]:
        """Returns a list of users in the room."""

        return self.db_pool.simple_select_onecol_txn(
            txn,
            table="current_state_events",
            keyvalues={
                "type": EventTypes.Member,
                "room_id": room_id,
                "membership": Membership.JOIN,
            },
            retcol="state_key",
        )

    async def get_invited_users_in_room(self, room_id: str) -> StrCollection:
        """Returns a list of users invited to the room."""
        return await self.db_pool.simple_select_onecol(
            table="current_state_events",
            keyvalues={
                "type": EventTypes.Member,
                "room_id": room_id,
                "membership": Membership.INVITE,
            },
            retcol="state_key",
            desc="get_invited_users_in_room",
        )

    @cached()
    def get_user_in_room_with_profile(self, room_id: str, user_id: str) -> ProfileInfo:
        raise NotImplementedError()

    @cachedList(
        cached_method_name="get_user_in_room_with_profile", list_name="user_ids"
    )
    async def get_subset_users_in_room_with_profiles(
        self, room_id: str, user_ids: Collection[str]
    ) -> Mapping[str, ProfileInfo]:
        """Get a mapping from user ID to profile information for a list of users
        in a given room.

        The profile information comes directly from this room's `m.room.member`
        events, and so may be specific to this room rather than part of a user's
        global profile. To avoid privacy leaks, the profile data should only be
        revealed to users who are already in this room.

        Args:
            room_id: The ID of the room to retrieve the users of.
            user_ids: a list of users in the room to run the query for

        Returns:
                A mapping from user ID to ProfileInfo.
        """

        def _get_subset_users_in_room_with_profiles(
            txn: LoggingTransaction,
        ) -> dict[str, ProfileInfo]:
            clause, ids = make_in_list_sql_clause(
                self.database_engine, "c.state_key", user_ids
            )

            sql = """
                SELECT state_key, display_name, avatar_url FROM room_memberships as m
                INNER JOIN current_state_events as c
                ON m.event_id = c.event_id
                AND m.room_id = c.room_id
                AND m.user_id = c.state_key
                WHERE c.type = 'm.room.member' AND c.room_id = ? AND m.membership = ? AND %s
            """ % (clause,)
            txn.execute(sql, (room_id, Membership.JOIN, *ids))

            return {r[0]: ProfileInfo(display_name=r[1], avatar_url=r[2]) for r in txn}

        return await self.db_pool.runInteraction(
            "get_subset_users_in_room_with_profiles",
            _get_subset_users_in_room_with_profiles,
        )

    @cached(max_entries=100000, iterable=True)
    async def get_users_in_room_with_profiles(
        self, room_id: str
    ) -> Mapping[str, ProfileInfo]:
        """Get a mapping from user ID to profile information for all users in a given room.

        The profile information comes directly from this room's `m.room.member`
        events, and so may be specific to this room rather than part of a user's
        global profile. To avoid privacy leaks, the profile data should only be
        revealed to users who are already in this room.

        Args:
            room_id: The ID of the room to retrieve the users of.

        Returns:
            A mapping from user ID to ProfileInfo.

        Preconditions:
          - There is full state available for the room (it is not partial-stated).
        """

        def _get_users_in_room_with_profiles(
            txn: LoggingTransaction,
        ) -> dict[str, ProfileInfo]:
            sql = """
                SELECT state_key, display_name, avatar_url FROM room_memberships as m
                INNER JOIN current_state_events as c
                ON m.event_id = c.event_id
                AND m.room_id = c.room_id
                AND m.user_id = c.state_key
                WHERE c.type = 'm.room.member' AND c.room_id = ? AND m.membership = ?
            """
            txn.execute(sql, (room_id, Membership.JOIN))

            return {r[0]: ProfileInfo(display_name=r[1], avatar_url=r[2]) for r in txn}

        return await self.db_pool.runInteraction(
            "get_users_in_room_with_profiles",
            _get_users_in_room_with_profiles,
        )

    @cached(max_entries=100000)  # type: ignore[synapse-@cached-mutable]
    async def get_room_summary(self, room_id: str) -> Mapping[str, MemberSummary]:
        """
        Get the details of a room roughly suitable for use by the room
        summary extension to /sync. Useful when lazy loading room members.

        Returns the total count of members in the room by membership type, and a
        truncated list of members (the heroes). This will be the first 6 members of the
        room:
        - We want 5 heroes plus 1, in case one of them is the
        calling user.
        - They are ordered by `stream_ordering`, which are joined or
        invited. When no joined or invited members are available, this also includes
        banned and left users.

        Args:
            room_id: The room ID to query
        Returns:
            dict of membership states, pointing to a MemberSummary named tuple.
        """

        def _get_room_summary_txn(
            txn: LoggingTransaction,
        ) -> dict[str, MemberSummary]:
            # first get counts.
            # We do this all in one transaction to keep the cache small.
            # FIXME: get rid of this when we have room_stats

            counts = self._get_member_counts_txn(txn, room_id)

            res: dict[str, MemberSummary] = {}
            for membership, count in counts.items():
                res.setdefault(membership, MemberSummary([], count))

            # Order by membership (joins -> invites -> leave (former insiders) ->
            # everything else (outsiders like bans/knocks), then by `stream_ordering` so
            # the first members in the room show up first and to make the sort stable
            # (consistent heroes).
            #
            # Note: rejected events will have a null membership field, so we we manually
            # filter them out.
            sql = """
                SELECT state_key, membership, event_id
                FROM current_state_events
                WHERE type = 'm.room.member' AND room_id = ?
                    AND membership IS NOT NULL
                ORDER BY
                    CASE membership WHEN ? THEN 1 WHEN ? THEN 2 WHEN ? THEN 3 ELSE 4 END ASC,
                    event_stream_ordering ASC
                LIMIT ?
            """

            txn.execute(
                sql,
                (
                    room_id,
                    # Sort order
                    Membership.JOIN,
                    Membership.INVITE,
                    Membership.LEAVE,
                    # 6 is 5 (number of heroes) plus 1, in case one of them is the calling user.
                    6,
                ),
            )
            for user_id, membership, event_id in txn:
                summary = res[membership]
                # we will always have a summary for this membership type at this
                # point given the summary currently contains the counts.
                members = summary.members
                members.append((user_id, event_id))

            return res

        return await self.db_pool.runInteraction(
            "get_room_summary", _get_room_summary_txn
        )

    @cached()
    async def get_member_counts(self, room_id: str) -> Mapping[str, int]:
        """Get a mapping of number of users by membership"""

        return await self.db_pool.runInteraction(
            "get_member_counts", self._get_member_counts_txn, room_id
        )

    def _get_member_counts_txn(
        self, txn: LoggingTransaction, room_id: str
    ) -> dict[str, int]:
        """Get a mapping of number of users by membership"""

        # Note, rejected events will have a null membership field, so
        # we we manually filter them out.
        sql = """
            SELECT count(*), membership FROM current_state_events
            WHERE type = 'm.room.member' AND room_id = ?
                AND membership IS NOT NULL
            GROUP BY membership
        """

        txn.execute(sql, (room_id,))
        return {membership: count for count, membership in txn}

    @cached()
    async def get_number_joined_users_in_room(self, room_id: str) -> int:
        return await self.db_pool.simple_select_one_onecol(
            table="current_state_events",
            keyvalues={"room_id": room_id, "membership": Membership.JOIN},
            retcol="COUNT(*)",
            desc="get_number_joined_users_in_room",
        )

    @cached()
    async def get_invited_rooms_for_local_user(
        self, user_id: str
    ) -> Sequence[RoomsForUser]:
        """Get all the rooms the *local* user is invited to.

        Args:
            user_id: The user ID.

        Returns:
            A list of RoomsForUser.
        """

        return await self.get_rooms_for_local_user_where_membership_is(
            user_id, [Membership.INVITE]
        )

    async def get_knocked_at_rooms_for_local_user(
        self, user_id: str
    ) -> Sequence[RoomsForUser]:
        """Get all the rooms the *local* user has knocked at.

        Args:
            user_id: The user ID.

        Returns:
            A list of RoomsForUser.
        """

        return await self.get_rooms_for_local_user_where_membership_is(
            user_id, [Membership.KNOCK]
        )

    async def get_invite_for_local_user_in_room(
        self, user_id: str, room_id: str
    ) -> RoomsForUser | None:
        """Gets the invite for the given *local* user and room.

        Args:
            user_id: The user ID to find the invite of.
            room_id: The room to user was invited to.

        Returns:
            Either a RoomsForUser or None if no invite was found.
        """
        invites = await self.get_invited_rooms_for_local_user(user_id)
        for invite in invites:
            if invite.room_id == room_id:
                return invite
        return None

    @trace
    async def get_rooms_for_local_user_where_membership_is(
        self,
        user_id: str,
        membership_list: Collection[str],
        excluded_rooms: StrCollection = (),
    ) -> list[RoomsForUser]:
        """Get all the rooms for this *local* user where the membership for this user
        matches one in the membership list.

        Filters out forgotten rooms.

        Args:
            user_id: The user ID.
            membership_list: A list of synapse.api.constants.Membership
                values which the user must be in.
            excluded_rooms: A list of rooms to ignore.

        Returns:
            The RoomsForUser that the user matches the membership types.
        """
        if not membership_list:
            return []

        # Convert membership list to frozen set as a) it needs to be hashable,
        # and b) we don't care about the order.
        membership_list = frozenset(membership_list)

        rooms = await self._get_rooms_for_local_user_where_membership_is_inner(
            user_id,
            membership_list,
        )

        # Now we filter out forgotten and excluded rooms
        rooms_to_exclude: AbstractSet[str] = set()

        # Users can't forget joined/invited rooms, so we skip the check for such look ups.
        if any(m not in (Membership.JOIN, Membership.INVITE) for m in membership_list):
            rooms_to_exclude = await self.get_forgotten_rooms_for_user(user_id)

        if excluded_rooms is not None:
            # Take a copy to avoid mutating the in-cache set
            rooms_to_exclude = set(rooms_to_exclude)
            rooms_to_exclude.update(excluded_rooms)

        return [room for room in rooms if room.room_id not in rooms_to_exclude]

    @cached(max_entries=1000, tree=True)
    async def _get_rooms_for_local_user_where_membership_is_inner(
        self,
        user_id: str,
        membership_list: Collection[str],
    ) -> Sequence[RoomsForUser]:
        if not membership_list:
            return []

        rooms = await self.db_pool.runInteraction(
            "get_rooms_for_local_user_where_membership_is",
            self._get_rooms_for_local_user_where_membership_is_txn,
            user_id,
            membership_list,
        )

        return rooms

    def _get_rooms_for_local_user_where_membership_is_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        membership_list: list[str],
    ) -> list[RoomsForUser]:
        """Get all the rooms for this *local* user where the membership for this user
        matches one in the membership list.

        Args:
            user_id: The user ID.
            membership_list: A list of synapse.api.constants.Membership
                    values which the user must be in.

        Returns:
            The RoomsForUser that the user matches the membership types.
        """
        # Paranoia check.
        if not self.hs.is_mine_id(user_id):
            raise Exception(
                "Cannot call 'get_rooms_for_local_user_where_membership_is' on non-local user %r"
                % (user_id,),
            )

        clause, args = make_in_list_sql_clause(
            self.database_engine, "c.membership", membership_list
        )

        sql = """
            SELECT room_id, e.sender, c.membership, event_id, e.instance_name, e.stream_ordering, r.room_version
            FROM local_current_membership AS c
            INNER JOIN events AS e USING (room_id, event_id)
            INNER JOIN rooms AS r USING (room_id)
            WHERE
                user_id = ?
                AND %s
        """ % (clause,)

        txn.execute(sql, (user_id, *args))
        results = [
            RoomsForUser(
                room_id=room_id,
                sender=sender,
                membership=membership,
                event_id=event_id,
                event_pos=PersistedEventPosition(
                    # If instance_name is null we default to "master"
                    instance_name or "master",
                    stream_ordering,
                ),
                room_version_id=room_version,
            )
            for room_id, sender, membership, event_id, instance_name, stream_ordering, room_version in txn
        ]

        return results

    @cached(iterable=True)
    async def get_local_users_in_room(self, room_id: str) -> Sequence[str]:
        """
        Retrieves a list of the current roommembers who are local to the server.
        """
        return await self.db_pool.simple_select_onecol(
            table="local_current_membership",
            keyvalues={"room_id": room_id, "membership": Membership.JOIN},
            retcol="user_id",
            desc="get_local_users_in_room",
        )

    async def get_local_users_related_to_room(
        self, room_id: str
    ) -> list[tuple[str, str]]:
        """
        Retrieves a list of the current roommembers who are local to the server and their membership status.
        """
        return cast(
            list[tuple[str, str]],
            await self.db_pool.simple_select_list(
                table="local_current_membership",
                keyvalues={"room_id": room_id},
                retcols=("user_id", "membership"),
                desc="get_local_users_in_room",
            ),
        )

    async def check_local_user_in_room(self, user_id: str, room_id: str) -> bool:
        """
        Check whether a given local user is currently joined to the given room.

        Returns:
            A boolean indicating whether the user is currently joined to the room

        Raises:
            Exeption when called with a non-local user to this homeserver
        """
        if not self.hs.is_mine_id(user_id):
            raise Exception(
                "Cannot call 'check_local_user_in_room' on "
                "non-local user %s" % (user_id,),
            )

        (
            membership,
            member_event_id,
        ) = await self.get_local_current_membership_for_user_in_room(
            user_id=user_id,
            room_id=room_id,
        )

        return membership == Membership.JOIN

    async def is_server_notice_room(self, room_id: str) -> bool:
        """
        Determines whether the given room is a 'Server Notices' room, used for
        sending server notices to a user.

        This is determined by seeing whether the server notices user is present
        in the room.
        """
        if self._server_notices_mxid is None:
            return False
        is_server_notices_room = await self.check_local_user_in_room(
            user_id=self._server_notices_mxid, room_id=room_id
        )
        return is_server_notices_room

    async def get_local_current_membership_for_user_in_room(
        self, user_id: str, room_id: str
    ) -> tuple[str | None, str | None]:
        """Retrieve the current local membership state and event ID for a user in a room.

        Args:
            user_id: The ID of the user.
            room_id: The ID of the room.

        Returns:
            A tuple of (membership_type, event_id). Both will be None if a
                room_id/user_id pair is not found.
        """
        # Paranoia check.
        if not self.hs.is_mine_id(user_id):
            message = f"Provided user_id {user_id} is a non-local user"
            raise SynapseError(HTTPStatus.BAD_REQUEST, message, errcode=Codes.BAD_JSON)

        results = cast(
            tuple[str, str] | None,
            await self.db_pool.simple_select_one(
                "local_current_membership",
                {"room_id": room_id, "user_id": user_id},
                ("membership", "event_id"),
                allow_none=True,
                desc="get_local_current_membership_for_user_in_room",
            ),
        )
        if not results:
            return None, None

        return results

    async def get_users_server_still_shares_room_with(
        self, user_ids: Collection[str]
    ) -> set[str]:
        """Given a list of users return the set that the server still share a
        room with.
        """

        if not user_ids:
            return set()

        return await self.db_pool.runInteraction(
            "get_users_server_still_shares_room_with",
            self.get_users_server_still_shares_room_with_txn,
            user_ids,
        )

    def get_users_server_still_shares_room_with_txn(
        self,
        txn: LoggingTransaction,
        user_ids: Collection[str],
    ) -> set[str]:
        if not user_ids:
            return set()

        sql = """
            SELECT state_key FROM current_state_events
            WHERE
                type = 'm.room.member'
                AND membership = 'join'
                AND %s
            GROUP BY state_key
        """

        clause, args = make_in_list_sql_clause(
            self.database_engine, "state_key", user_ids
        )

        txn.execute(sql % (clause,), args)

        return {row[0] for row in txn}

    async def get_rooms_user_currently_banned_from(
        self, user_id: str
    ) -> frozenset[str]:
        """Returns a set of room_ids the user is currently banned from.

        If a remote user only returns rooms this server is currently
        participating in.
        """
        room_ids = await self.db_pool.simple_select_onecol(
            table="current_state_events",
            keyvalues={
                "type": EventTypes.Member,
                "membership": Membership.BAN,
                "state_key": user_id,
            },
            retcol="room_id",
            desc="get_rooms_user_currently_banned_from",
        )

        return frozenset(room_ids)

    @cached(max_entries=500000, iterable=True)
    async def get_rooms_for_user(self, user_id: str) -> frozenset[str]:
        """Returns a set of room_ids the user is currently joined to.

        If a remote user only returns rooms this server is currently
        participating in.
        """

        room_ids = await self.db_pool.simple_select_onecol(
            table="current_state_events",
            keyvalues={
                "type": EventTypes.Member,
                "membership": Membership.JOIN,
                "state_key": user_id,
            },
            retcol="room_id",
            desc="get_rooms_for_user",
        )

        return frozenset(room_ids)

    @cachedList(
        cached_method_name="get_rooms_for_user",
        list_name="user_ids",
    )
    async def _get_rooms_for_users(
        self, user_ids: Collection[str]
    ) -> Mapping[str, frozenset[str]]:
        """A batched version of `get_rooms_for_user`.

        Returns:
            Map from user_id to set of rooms that is currently in.
        """

        rows = cast(
            list[tuple[str, str]],
            await self.db_pool.simple_select_many_batch(
                table="current_state_events",
                column="state_key",
                iterable=user_ids,
                retcols=(
                    "state_key",
                    "room_id",
                ),
                keyvalues={
                    "type": EventTypes.Member,
                    "membership": Membership.JOIN,
                },
                desc="get_rooms_for_users",
            ),
        )

        user_rooms: dict[str, set[str]] = {user_id: set() for user_id in user_ids}

        for state_key, room_id in rows:
            user_rooms[state_key].add(room_id)

        return {key: frozenset(rooms) for key, rooms in user_rooms.items()}

    async def get_rooms_for_users(
        self, user_ids: Collection[str]
    ) -> dict[str, frozenset[str]]:
        """A batched wrapper around `_get_rooms_for_users`, to prevent locking
        other calls to `get_rooms_for_user` for large user lists.
        """
        all_user_rooms: dict[str, frozenset[str]] = {}

        # 250 users is pretty arbitrary but the data can be quite large if users
        # are in many rooms.
        for batch_user_ids in batch_iter(user_ids, 250):
            all_user_rooms.update(await self._get_rooms_for_users(batch_user_ids))

        return all_user_rooms

    @cached(max_entries=10000)
    async def does_pair_of_users_share_a_room(
        self, user_id: str, other_user_id: str
    ) -> bool:
        raise NotImplementedError()

    @cachedList(
        cached_method_name="does_pair_of_users_share_a_room", list_name="other_user_ids"
    )
    async def _do_users_share_a_room(
        self, user_id: str, other_user_ids: Collection[str]
    ) -> Mapping[str, bool | None]:
        """Return mapping from user ID to whether they share a room with the
        given user.

        Note: `None` and `False` are equivalent and mean they don't share a
        room.
        """

        def do_users_share_a_room_txn(
            txn: LoggingTransaction, user_ids: Collection[str]
        ) -> dict[str, bool]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "state_key", user_ids
            )

            # This query works by fetching both the list of rooms for the target
            # user and the set of other users, and then checking if there is any
            # overlap.
            sql = f"""
                SELECT DISTINCT b.state_key
                FROM (
                    SELECT room_id FROM current_state_events
                    WHERE type = 'm.room.member' AND membership = 'join' AND state_key = ?
                ) AS a
                INNER JOIN (
                    SELECT room_id, state_key FROM current_state_events
                    WHERE type = 'm.room.member' AND membership = 'join' AND {clause}
                ) AS b using (room_id)
            """

            txn.execute(sql, (user_id, *args))
            return {u: True for (u,) in txn}

        to_return = {}
        for batch_user_ids in batch_iter(other_user_ids, 1000):
            res = await self.db_pool.runInteraction(
                "do_users_share_a_room", do_users_share_a_room_txn, batch_user_ids
            )
            to_return.update(res)

        return to_return

    async def do_users_share_a_room(
        self, user_id: str, other_user_ids: Collection[str]
    ) -> set[str]:
        """Return the set of users who share a room with the first users"""

        user_dict = await self._do_users_share_a_room(user_id, other_user_ids)

        return {u for u, share_room in user_dict.items() if share_room}

    @cached(max_entries=10000)
    async def does_pair_of_users_share_a_room_joined_or_invited(
        self, user_id: str, other_user_id: str
    ) -> bool:
        raise NotImplementedError()

    @cachedList(
        cached_method_name="does_pair_of_users_share_a_room_joined_or_invited",
        list_name="other_user_ids",
    )
    async def _do_users_share_a_room_joined_or_invited(
        self, user_id: str, other_user_ids: Collection[str]
    ) -> Mapping[str, bool | None]:
        """Return mapping from user ID to whether they share a room with the
        given user via being either joined or invited.

        Note: `None` and `False` are equivalent and mean they don't share a
        room.
        """

        def do_users_share_a_room_joined_or_invited_txn(
            txn: LoggingTransaction, user_ids: Collection[str]
        ) -> dict[str, bool]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "state_key", user_ids
            )

            # This query works by fetching both the list of rooms for the target
            # user and the set of other users, and then checking if there is any
            # overlap.
            sql = f"""
                SELECT DISTINCT b.state_key
                FROM (
                    SELECT room_id FROM current_state_events
                    WHERE type = 'm.room.member' AND (membership = 'join' OR membership = 'invite') AND state_key = ?
                ) AS a
                INNER JOIN (
                    SELECT room_id, state_key FROM current_state_events
                    WHERE type = 'm.room.member' AND (membership = 'join' OR membership = 'invite') AND {clause}
                ) AS b using (room_id)
            """

            txn.execute(sql, (user_id, *args))
            return {u: True for (u,) in txn}

        to_return = {}
        for batch_user_ids in batch_iter(other_user_ids, 1000):
            res = await self.db_pool.runInteraction(
                "do_users_share_a_room_joined_or_invited",
                do_users_share_a_room_joined_or_invited_txn,
                batch_user_ids,
            )
            to_return.update(res)

        return to_return

    async def do_users_share_a_room_joined_or_invited(
        self, user_id: str, other_user_ids: Collection[str]
    ) -> set[str]:
        """Return the set of users who share a room with the first users via being either joined or invited"""

        user_dict = await self._do_users_share_a_room_joined_or_invited(
            user_id, other_user_ids
        )

        return {u for u, share_room in user_dict.items() if share_room}

    async def get_users_who_share_room_with_user(self, user_id: str) -> set[str]:
        """Returns the set of users who share a room with `user_id`"""
        room_ids = await self.get_rooms_for_user(user_id)

        user_who_share_room: set[str] = set()
        for room_id in room_ids:
            user_ids = await self.get_users_in_room(room_id)
            user_who_share_room.update(user_ids)

        return user_who_share_room

    @cached(cache_context=True, iterable=True)
    async def get_mutual_rooms_between_users(
        self, user_ids: frozenset[str], cache_context: _CacheContext
    ) -> frozenset[str]:
        """
        Returns the set of rooms that all users in `user_ids` share.

        Args:
            user_ids: A frozen set of all users to investigate and return
              overlapping joined rooms for.
            cache_context
        """
        shared_room_ids: frozenset[str] | None = None
        for user_id in user_ids:
            room_ids = await self.get_rooms_for_user(
                user_id, on_invalidate=cache_context.invalidate
            )
            if shared_room_ids is not None:
                shared_room_ids &= room_ids
            else:
                shared_room_ids = room_ids

        return shared_room_ids or frozenset()

    async def get_joined_user_ids_from_state(
        self, room_id: str, state: StateMap[str]
    ) -> set[str]:
        """
        For a given set of state IDs, get a set of user IDs in the room.

        This method checks the local event cache, before calling
        `_get_user_ids_from_membership_event_ids` for any uncached events.
        """

        with Measure(
            self.clock,
            name="get_joined_user_ids_from_state",
            server_name=self.server_name,
        ):
            users_in_room = set()
            member_event_ids = [
                e_id for key, e_id in state.items() if key[0] == EventTypes.Member
            ]

            # We check if we have any of the member event ids in the event cache
            # before we ask the DB

            # We don't update the event cache hit ratio as it completely throws off
            # the hit ratio counts. After all, we don't populate the cache if we
            # miss it here
            event_map = self._get_events_from_local_cache(
                member_event_ids, update_metrics=False
            )

            missing_member_event_ids = []
            for event_id in member_event_ids:
                ev_entry = event_map.get(event_id)
                if ev_entry and not ev_entry.event.rejected_reason:
                    if ev_entry.event.membership == Membership.JOIN:
                        users_in_room.add(ev_entry.event.state_key)
                else:
                    missing_member_event_ids.append(event_id)

            if missing_member_event_ids:
                event_to_memberships = (
                    await self._get_user_ids_from_membership_event_ids(
                        missing_member_event_ids
                    )
                )
                users_in_room.update(
                    user_id for user_id in event_to_memberships.values() if user_id
                )

            return users_in_room

    @cached(
        max_entries=10000,
        # This name matches the old function that has been replaced - the cache name
        # is kept here to maintain backwards compatibility.
        name="_get_joined_profile_from_event_id",
    )
    def _get_user_id_from_membership_event_id(
        self, event_id: str
    ) -> tuple[str, ProfileInfo] | None:
        raise NotImplementedError()

    @cachedList(
        cached_method_name="_get_user_id_from_membership_event_id",
        list_name="event_ids",
    )
    async def _get_user_ids_from_membership_event_ids(
        self, event_ids: Iterable[str]
    ) -> Mapping[str, str | None]:
        """For given set of member event_ids check if they point to a join
        event.

        Args:
            event_ids: The member event IDs to lookup

        Returns:
            Map from event ID to `user_id`, or None if event is not a join.
        """

        rows = cast(
            list[tuple[str, str]],
            await self.db_pool.simple_select_many_batch(
                table="room_memberships",
                column="event_id",
                iterable=event_ids,
                retcols=("event_id", "user_id"),
                keyvalues={"membership": Membership.JOIN},
                batch_size=1000,
                desc="_get_user_ids_from_membership_event_ids",
            ),
        )

        return dict(rows)

    @cached(max_entries=10000)
    async def is_host_joined(self, room_id: str, host: str) -> bool:
        return await self._check_host_room_membership(room_id, host, Membership.JOIN)

    @cached(max_entries=10000)
    async def is_host_invited(self, room_id: str, host: str) -> bool:
        return await self._check_host_room_membership(room_id, host, Membership.INVITE)

    async def _check_host_room_membership(
        self, room_id: str, host: str, membership: str
    ) -> bool:
        if "%" in host or "_" in host:
            raise Exception("Invalid host name")

        sql = """
            SELECT state_key FROM current_state_events
            WHERE membership = ?
                AND type = 'm.room.member'
                AND room_id = ?
                AND state_key LIKE ?
            LIMIT 1
        """

        # We do need to be careful to ensure that host doesn't have any wild cards
        # in it, but we checked above for known ones and we'll check below that
        # the returned user actually has the correct domain.
        like_clause = "%:" + host

        rows = await self.db_pool.execute(
            "is_host_joined", sql, membership, room_id, like_clause
        )

        if not rows:
            return False

        user_id = rows[0][0]
        if get_domain_from_id(user_id) != host:
            # This can only happen if the host name has something funky in it
            raise Exception("Invalid host name")

        return True

    @cached(iterable=True, max_entries=10000)
    async def get_current_hosts_in_room(self, room_id: str) -> AbstractSet[str]:
        """Get current hosts in room based on current state."""

        # First we check if we already have `get_users_in_room` in the cache, as
        # we can just calculate result from that
        users = self.get_users_in_room.cache.get_immediate(
            (room_id,), None, update_metrics=False
        )
        if users is not None:
            return {get_domain_from_id(u) for u in users}

        if isinstance(self.database_engine, Sqlite3Engine):
            # If we're using SQLite then let's just always use
            # `get_users_in_room` rather than funky SQL.
            users = await self.get_users_in_room(room_id)
            return {get_domain_from_id(u) for u in users}

        # For PostgreSQL we can use a regex to pull out the domains from the
        # joined users in `current_state_events` via regex.

        def get_current_hosts_in_room_txn(txn: LoggingTransaction) -> set[str]:
            sql = """
                SELECT DISTINCT substring(state_key FROM '@[^:]*:(.*)$')
                FROM current_state_events
                WHERE
                    type = 'm.room.member'
                    AND membership = 'join'
                    AND room_id = ?
            """
            txn.execute(sql, (room_id,))
            return {d for (d,) in txn}

        return await self.db_pool.runInteraction(
            "get_current_hosts_in_room", get_current_hosts_in_room_txn
        )

    @cached(iterable=True, max_entries=10000)
    async def get_current_hosts_in_room_ordered(self, room_id: str) -> tuple[str, ...]:
        """
        Get current hosts in room based on current state.

        The heuristic of sorting by servers who have been in the room the
        longest is good because they're most likely to have anything we ask
        about.

        For SQLite the returned list is not ordered, as SQLite doesn't support
        the appropriate SQL.

        Uses `m.room.member`s in the room state at the current forward
        extremities to determine which hosts are in the room.

        Will return inaccurate results for rooms with partial state, since the
        state for the forward extremities of those rooms will exclude most
        members. We may also calculate room state incorrectly for such rooms and
        believe that a host is or is not in the room when the opposite is true.

        Returns:
            Returns a list of servers sorted by longest in the room first. (aka.
            sorted by join with the lowest depth first).
        """

        if isinstance(self.database_engine, Sqlite3Engine):
            # If we're using SQLite then let's just always use
            # `get_users_in_room` rather than funky SQL.

            domains = await self.get_current_hosts_in_room(room_id)
            return tuple(domains)

        # For PostgreSQL we can use a regex to pull out the domains from the
        # joined users in `current_state_events` via regex.

        def get_current_hosts_in_room_ordered_txn(
            txn: LoggingTransaction,
        ) -> tuple[str, ...]:
            # Returns a list of servers currently joined in the room sorted by
            # longest in the room first (aka. with the lowest depth). The
            # heuristic of sorting by servers who have been in the room the
            # longest is good because they're most likely to have anything we
            # ask about.
            sql = """
                SELECT
                    /* Match the domain part of the MXID */
                    substring(c.state_key FROM '@[^:]*:(.*)$') as server_domain
                FROM current_state_events c
                /* Get the depth of the event from the events table */
                INNER JOIN events AS e USING (event_id)
                WHERE
                    /* Find any join state events in the room */
                    c.type = 'm.room.member'
                    AND c.membership = 'join'
                    AND c.room_id = ?
                /* Group all state events from the same domain into their own buckets (groups) */
                GROUP BY server_domain
                /* Sorted by lowest depth first */
                ORDER BY min(e.depth) ASC;
            """
            txn.execute(sql, (room_id,))
            # `server_domain` will be `NULL` for malformed MXIDs with no colons.
            return tuple(d for (d,) in txn if d is not None)

        return await self.db_pool.runInteraction(
            "get_current_hosts_in_room_ordered", get_current_hosts_in_room_ordered_txn
        )

    async def _get_approximate_current_memberships_in_room(
        self, room_id: str
    ) -> Mapping[str, str | None]:
        """Build a map from event id to membership, for all events in the current state.

        The event ids of non-memberships events (e.g. `m.room.power_levels`) are present
        in the result, mapped to values of `None`.

        The result is approximate for partially-joined rooms. It is fully accurate
        for fully-joined rooms.
        """

        rows = cast(
            list[tuple[str, str | None]],
            await self.db_pool.simple_select_list(
                "current_state_events",
                keyvalues={"room_id": room_id},
                retcols=("event_id", "membership"),
                desc="has_completed_background_updates",
            ),
        )
        return dict(rows)

    # TODO This returns a mutable object, which is generally confusing when using a cache.
    @cached(max_entries=10000)  # type: ignore[synapse-@cached-mutable]
    def _get_joined_hosts_cache(self, room_id: str) -> "_JoinedHostsCache":
        return _JoinedHostsCache()

    @cached(num_args=2)
    async def did_forget(self, user_id: str, room_id: str) -> bool:
        """Returns whether user_id has elected to discard history for room_id.

        Returns False if they have since re-joined."""

        def f(txn: LoggingTransaction) -> int:
            sql = (
                "SELECT"
                "  COUNT(*)"
                " FROM"
                "  room_memberships"
                " WHERE"
                "  user_id = ?"
                " AND"
                "  room_id = ?"
                " AND"
                "  forgotten = 0"
            )
            txn.execute(sql, (user_id, room_id))
            rows = txn.fetchall()
            return rows[0][0]

        count = await self.db_pool.runInteraction("did_forget_membership", f)
        return count == 0

    @cached()
    async def get_forgotten_rooms_for_user(self, user_id: str) -> AbstractSet[str]:
        """Gets all rooms the user has forgotten.

        Args:
            user_id: The user ID to query the rooms of.

        Returns:
            The forgotten rooms.
        """

        def _get_forgotten_rooms_for_user_txn(txn: LoggingTransaction) -> set[str]:
            # This is a slightly convoluted query that first looks up all rooms
            # that the user has forgotten in the past, then rechecks that list
            # to see if any have subsequently been updated. This is done so that
            # we can use a partial index on `forgotten = 1` on the assumption
            # that few users will actually forget many rooms.
            #
            # Note that a room is considered "forgotten" if *all* membership
            # events for that user and room have the forgotten field set (as
            # when a user forgets a room we update all rows for that user and
            # room, not just the current one).
            sql = """
                SELECT room_id, (
                    SELECT count(*) FROM room_memberships
                    WHERE room_id = m.room_id AND user_id = m.user_id AND forgotten = 0
                ) AS count
                FROM room_memberships AS m
                WHERE user_id = ? AND forgotten = 1
                GROUP BY room_id, user_id;
            """
            txn.execute(sql, (user_id,))
            return {row[0] for row in txn if row[1] == 0}

        return await self.db_pool.runInteraction(
            "get_forgotten_rooms_for_user", _get_forgotten_rooms_for_user_txn
        )

    async def is_locally_forgotten_room(self, room_id: str) -> bool:
        """Returns whether all local users have forgotten this room_id.

        Args:
            room_id: The room ID to query.

        Returns:
            Whether the room is forgotten.
        """

        sql = """
            SELECT count(*) > 0 FROM local_current_membership
            INNER JOIN room_memberships USING (room_id, event_id)
            WHERE
                room_id = ?
                AND forgotten = 0;
        """

        rows = await self.db_pool.execute("is_forgotten_room", sql, room_id)

        # `count(*)` returns always an integer
        # If any rows still exist it means someone has not forgotten this room yet
        return not rows[0][0]

    async def get_rooms_user_has_been_in(self, user_id: str) -> set[str]:
        """Get all rooms that the user has ever been in.

        Args:
            user_id: The user ID to get the rooms of.

        Returns:
            Set of room IDs.
        """

        room_ids = await self.db_pool.simple_select_onecol(
            table="room_memberships",
            keyvalues={"membership": Membership.JOIN, "user_id": user_id},
            retcol="room_id",
            desc="get_rooms_user_has_been_in",
        )

        return set(room_ids)

    async def get_membership_event_ids_for_user(
        self, user_id: str, room_id: str
    ) -> set[str]:
        """Get all event_ids for the given user and room.

        Args:
            user_id: The user ID to get the event IDs for.
            room_id: The room ID to look up events for.

        Returns:
            Set of event IDs
        """

        event_ids = await self.db_pool.simple_select_onecol(
            table="room_memberships",
            keyvalues={"user_id": user_id, "room_id": room_id},
            retcol="event_id",
            desc="get_membership_event_ids_for_user",
        )

        return set(event_ids)

    @cached(max_entries=5000)
    async def _get_membership_from_event_id(
        self, member_event_id: str
    ) -> EventIdMembership | None:
        raise NotImplementedError()

    @cachedList(
        cached_method_name="_get_membership_from_event_id", list_name="member_event_ids"
    )
    async def get_membership_from_event_ids(
        self, member_event_ids: Iterable[str]
    ) -> Mapping[str, EventIdMembership | None]:
        """Get user_id and membership of a set of event IDs.

        Returns:
            Mapping from event ID to `EventIdMembership` if the event is a
            membership event, otherwise the value is None.
        """

        rows = cast(
            list[tuple[str, str, str]],
            await self.db_pool.simple_select_many_batch(
                table="room_memberships",
                column="event_id",
                iterable=member_event_ids,
                retcols=("user_id", "membership", "event_id"),
                keyvalues={},
                batch_size=500,
                desc="get_membership_from_event_ids",
            ),
        )

        return {
            event_id: EventIdMembership(membership=membership, user_id=user_id)
            for user_id, membership, event_id in rows
        }

    async def is_local_host_in_room_ignoring_users(
        self, room_id: str, ignore_users: Collection[str]
    ) -> bool:
        """Check if there are any local users, excluding those in the given
        list, in the room.
        """

        clause, args = make_in_list_sql_clause(
            self.database_engine, "user_id", ignore_users
        )

        sql = """
            SELECT 1 FROM local_current_membership
            WHERE
                room_id = ? AND membership = ?
                AND NOT (%s)
                LIMIT 1
        """ % (clause,)

        def _is_local_host_in_room_ignoring_users_txn(
            txn: LoggingTransaction,
        ) -> bool:
            txn.execute(sql, (room_id, Membership.JOIN, *args))

            return bool(txn.fetchone())

        return await self.db_pool.runInteraction(
            "is_local_host_in_room_ignoring_users",
            _is_local_host_in_room_ignoring_users_txn,
        )

    async def forget(self, user_id: str, room_id: str) -> None:
        """Indicate that user_id wishes to discard history for room_id."""

        def f(txn: LoggingTransaction) -> None:
            self.db_pool.simple_update_txn(
                txn,
                table="room_memberships",
                keyvalues={"user_id": user_id, "room_id": room_id},
                updatevalues={"forgotten": 1},
            )
            # Handle updating the `sliding_sync_membership_snapshots` table
            self.db_pool.simple_update_txn(
                txn,
                table="sliding_sync_membership_snapshots",
                keyvalues={"user_id": user_id, "room_id": room_id},
                updatevalues={"forgotten": 1},
            )

            self._invalidate_cache_and_stream(txn, self.did_forget, (user_id, room_id))
            self._invalidate_cache_and_stream(
                txn, self.get_forgotten_rooms_for_user, (user_id,)
            )
            self._invalidate_cache_and_stream(
                txn,
                self.get_sliding_sync_rooms_for_user_from_membership_snapshots,
                (user_id,),
            )

        await self.db_pool.runInteraction("forget_membership", f)

    async def get_room_forgetter_stream_pos(self) -> int:
        """Get the stream position of the background process to forget rooms when left
        by users.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="room_forgetter_stream_pos",
            keyvalues={},
            retcol="stream_id",
            desc="room_forgetter_stream_pos",
        )

    async def update_room_forgetter_stream_pos(self, stream_id: int) -> None:
        """Update the stream position of the background process to forget rooms when
        left by users.

        Must only be used by the worker running the background process.
        """
        assert self.hs.config.worker.run_background_tasks

        await self.db_pool.simple_update_one(
            table="room_forgetter_stream_pos",
            keyvalues={},
            updatevalues={"stream_id": stream_id},
            desc="room_forgetter_stream_pos",
        )

    @cached(iterable=True, max_entries=10000)
    async def get_sliding_sync_rooms_for_user_from_membership_snapshots(
        self, user_id: str
    ) -> Mapping[str, RoomsForUserSlidingSync]:
        """
        Get all the rooms for a user to handle a sliding sync request from the
        `sliding_sync_membership_snapshots` table. These will be current memberships and
        need to be rewound to the token range.

        Ignores forgotten rooms and rooms that the user has left themselves.

        Args:
            user_id: The user ID to get the rooms for.

        Returns:
            Map from room ID to membership info
        """

        def _txn(
            txn: LoggingTransaction,
        ) -> dict[str, RoomsForUserSlidingSync]:
            # XXX: If you use any new columns that can change (like from
            # `sliding_sync_joined_rooms` or `forgotten`), make sure to bust the
            # `get_sliding_sync_rooms_for_user_from_membership_snapshots` cache in the
            # appropriate places (and add tests).
            sql = """
                SELECT m.room_id, m.sender, m.membership, m.membership_event_id,
                    r.room_version,
                    m.event_instance_name, m.event_stream_ordering,
                    m.has_known_state,
                    COALESCE(j.room_type, m.room_type),
                    COALESCE(j.is_encrypted, m.is_encrypted)
                FROM sliding_sync_membership_snapshots AS m
                INNER JOIN rooms AS r USING (room_id)
                LEFT JOIN sliding_sync_joined_rooms AS j ON (j.room_id = m.room_id AND m.membership = 'join')
                WHERE user_id = ?
                    AND m.forgotten = 0
                    AND (m.membership != 'leave' OR m.user_id != m.sender)
            """
            txn.execute(sql, (user_id,))

            return {
                row[0]: RoomsForUserSlidingSync(
                    room_id=row[0],
                    sender=row[1],
                    membership=row[2],
                    event_id=row[3],
                    room_version_id=row[4],
                    event_pos=PersistedEventPosition(row[5], row[6]),
                    has_known_state=bool(row[7]),
                    room_type=row[8],
                    is_encrypted=bool(row[9]),
                )
                for row in txn
                # We filter out unknown room versions proactively. They
                # shouldn't go down sync and their metadata may be in a broken
                # state (causing errors).
                if row[4] in KNOWN_ROOM_VERSIONS
            }

        return await self.db_pool.runInteraction(
            "get_sliding_sync_rooms_for_user_from_membership_snapshots",
            _txn,
        )

    async def get_sliding_sync_self_leave_rooms_after_to_token(
        self,
        user_id: str,
        to_token: StreamToken,
    ) -> dict[str, RoomsForUserSlidingSync]:
        """
        Get all the self-leave rooms for a user after the `to_token` (outside the token
        range) that are potentially relevant[1] and needed to handle a sliding sync
        request. The results are from the `sliding_sync_membership_snapshots` table and
        will be current memberships and need to be rewound to the token range.

        [1] If a leave happens after the token range, we may have still been joined (or
        any non-self-leave which is relevant to sync) to the room before so we need to
        include it in the list of potentially relevant rooms and apply
        our rewind logic (outside of this function) to see if it's actually relevant.

        This is basically a sister-function to
        `get_sliding_sync_rooms_for_user_from_membership_snapshots`. We could
        alternatively incorporate this logic into
        `get_sliding_sync_rooms_for_user_from_membership_snapshots` but those results
        are cached and the `to_token` isn't very cache friendly (people are constantly
        requesting with new tokens) so we separate it out here.

        Args:
            user_id: The user ID to get the rooms for.
            to_token: Any self-leave memberships after this position will be returned.

        Returns:
            Map from room ID to membership info
        """
        # TODO: Potential to check
        # `self._membership_stream_cache.has_entity_changed(...)` as an early-return
        # shortcut.

        def _txn(
            txn: LoggingTransaction,
        ) -> dict[str, RoomsForUserSlidingSync]:
            sql = """
                SELECT m.room_id, m.sender, m.membership, m.membership_event_id,
                    r.room_version,
                    m.event_instance_name, m.event_stream_ordering,
                    m.has_known_state,
                    m.room_type,
                    m.is_encrypted
                FROM sliding_sync_membership_snapshots AS m
                INNER JOIN rooms AS r USING (room_id)
                WHERE user_id = ?
                    AND m.forgotten = 0
                    AND m.membership = 'leave'
                    AND m.user_id = m.sender
                    AND (m.event_stream_ordering > ?)
            """
            # If a leave happens after the token range, we may have still been joined
            # (or any non-self-leave which is relevant to sync) to the room before so we
            # need to include it in the list of potentially relevant rooms and apply our
            # rewind logic (outside of this function).
            #
            # To handle tokens with a non-empty instance_map we fetch more
            # results than necessary and then filter down
            min_to_token_position = to_token.room_key.stream
            txn.execute(sql, (user_id, min_to_token_position))

            # Map from room_id to membership info
            room_membership_for_user_map: dict[str, RoomsForUserSlidingSync] = {}
            for row in txn:
                room_for_user = RoomsForUserSlidingSync(
                    room_id=row[0],
                    sender=row[1],
                    membership=row[2],
                    event_id=row[3],
                    room_version_id=row[4],
                    event_pos=PersistedEventPosition(row[5], row[6]),
                    has_known_state=bool(row[7]),
                    room_type=row[8],
                    is_encrypted=bool(row[9]),
                )

                # We filter out unknown room versions proactively. They shouldn't go
                # down sync and their metadata may be in a broken state (causing
                # errors).
                if row[4] not in KNOWN_ROOM_VERSIONS:
                    continue

                # We only want to include the self-leave membership if it happened after
                # the token range.
                #
                # Since the database pulls out more than necessary, we need to filter it
                # down here.
                if _filter_results_by_stream(
                    lower_token=None,
                    upper_token=to_token.room_key,
                    instance_name=room_for_user.event_pos.instance_name,
                    stream_ordering=room_for_user.event_pos.stream,
                ):
                    continue

                room_membership_for_user_map[room_for_user.room_id] = room_for_user

            return room_membership_for_user_map

        return await self.db_pool.runInteraction(
            "get_sliding_sync_self_leave_rooms_after_to_token",
            _txn,
        )

    async def get_sliding_sync_room_for_user(
        self, user_id: str, room_id: str
    ) -> RoomsForUserSlidingSync | None:
        """Get the sliding sync room entry for the given user and room."""

        def get_sliding_sync_room_for_user_txn(
            txn: LoggingTransaction,
        ) -> RoomsForUserSlidingSync | None:
            sql = """
                SELECT m.room_id, m.sender, m.membership, m.membership_event_id,
                    r.room_version,
                    m.event_instance_name, m.event_stream_ordering,
                    m.has_known_state,
                    COALESCE(j.room_type, m.room_type),
                    COALESCE(j.is_encrypted, m.is_encrypted)
                FROM sliding_sync_membership_snapshots AS m
                INNER JOIN rooms AS r USING (room_id)
                LEFT JOIN sliding_sync_joined_rooms AS j ON (j.room_id = m.room_id AND m.membership = 'join')
                WHERE user_id = ?
                    AND m.forgotten = 0
                    AND m.room_id = ?
            """
            txn.execute(sql, (user_id, room_id))
            row = txn.fetchone()
            if not row:
                return None

            return RoomsForUserSlidingSync(
                room_id=row[0],
                sender=row[1],
                membership=row[2],
                event_id=row[3],
                room_version_id=row[4],
                event_pos=PersistedEventPosition(row[5], row[6]),
                has_known_state=bool(row[7]),
                room_type=row[8],
                is_encrypted=row[9],
            )

        return await self.db_pool.runInteraction(
            "get_sliding_sync_room_for_user", get_sliding_sync_room_for_user_txn
        )

    async def get_sliding_sync_room_for_user_batch(
        self, user_id: str, room_ids: StrCollection
    ) -> dict[str, RoomsForUserSlidingSync]:
        """Get the sliding sync room entry for the given user and rooms."""

        if not room_ids:
            return {}

        def get_sliding_sync_room_for_user_batch_txn(
            txn: LoggingTransaction,
        ) -> dict[str, RoomsForUserSlidingSync]:
            clause, args = make_in_list_sql_clause(
                self.database_engine, "m.room_id", room_ids
            )
            sql = f"""
                SELECT m.room_id, m.sender, m.membership, m.membership_event_id,
                    r.room_version,
                    m.event_instance_name, m.event_stream_ordering,
                    m.has_known_state,
                    COALESCE(j.room_type, m.room_type),
                    COALESCE(j.is_encrypted, m.is_encrypted)
                FROM sliding_sync_membership_snapshots AS m
                INNER JOIN rooms AS r USING (room_id)
                LEFT JOIN sliding_sync_joined_rooms AS j ON (j.room_id = m.room_id AND m.membership = 'join')
                WHERE m.forgotten = 0
                    AND {clause}
                    AND user_id = ?
            """
            args.append(user_id)
            txn.execute(sql, args)

            return {
                row[0]: RoomsForUserSlidingSync(
                    room_id=row[0],
                    sender=row[1],
                    membership=row[2],
                    event_id=row[3],
                    room_version_id=row[4],
                    event_pos=PersistedEventPosition(row[5], row[6]),
                    has_known_state=bool(row[7]),
                    room_type=row[8],
                    is_encrypted=row[9],
                )
                for row in txn
            }

        return await self.db_pool.runInteraction(
            "get_sliding_sync_room_for_user_batch",
            get_sliding_sync_room_for_user_batch_txn,
        )

    async def get_rooms_for_user_by_date(
        self, user_id: str, from_ts: int
    ) -> frozenset[str]:
        """
        Fetch a list of rooms that the user has joined at or after the given timestamp, including
        those they subsequently have left/been banned from.

        Args:
            user_id: user ID of the user to search for
            from_ts: a timestamp in ms from the unix epoch at which to begin the search at
        """

        def _get_rooms_for_user_by_join_date_txn(
            txn: LoggingTransaction, user_id: str, timestamp: int
        ) -> frozenset:
            sql = """
                SELECT rm.room_id
                FROM room_memberships AS rm
                INNER JOIN events AS e USING (event_id)
                WHERE rm.user_id = ?
                    AND rm.membership = 'join'
                    AND e.type = 'm.room.member'
                    AND e.received_ts >= ?
            """
            txn.execute(sql, (user_id, timestamp))
            return frozenset([r[0] for r in txn])

        return await self.db_pool.runInteraction(
            "_get_rooms_for_user_by_join_date_txn",
            _get_rooms_for_user_by_join_date_txn,
            user_id,
            from_ts,
        )

    async def set_room_participation(self, user_id: str, room_id: str) -> None:
        """
        Record the provided user as participating in the given room

        Args:
            user_id: the user ID of the user
            room_id: ID of the room to set the participant in
        """

        def _set_room_participation_txn(
            txn: LoggingTransaction, user_id: str, room_id: str
        ) -> None:
            sql = """
                UPDATE room_memberships
                SET participant = true
                WHERE event_id IN (
                    SELECT event_id FROM local_current_membership
                    WHERE user_id = ? AND room_id = ?
                )
                AND NOT participant
            """
            txn.execute(sql, (user_id, room_id))

        await self.db_pool.runInteraction(
            "_set_room_participation_txn", _set_room_participation_txn, user_id, room_id
        )

    async def get_room_participation(self, user_id: str, room_id: str) -> bool:
        """
        Check whether a user is listed as a participant in a room

        Args:
            user_id: user ID of the user
            room_id: ID of the room to check in
        """

        def _get_room_participation_txn(
            txn: LoggingTransaction, user_id: str, room_id: str
        ) -> bool:
            sql = """
                SELECT participant
                FROM local_current_membership AS l
                INNER JOIN room_memberships AS r USING (event_id)
                WHERE l.user_id = ?
                AND l.room_id = ?
            """
            txn.execute(sql, (user_id, room_id))
            res = txn.fetchone()
            if res:
                return res[0]
            return False

        return await self.db_pool.runInteraction(
            "_get_room_participation_txn", _get_room_participation_txn, user_id, room_id
        )

    async def get_ban_event_ids_in_room(self, room_id: str) -> StrCollection:
        """Get all event IDs for ban events in the given room."""
        return await self.db_pool.simple_select_onecol(
            table="current_state_events",
            keyvalues={
                "room_id": room_id,
                "type": EventTypes.Member,
                "membership": Membership.BAN,
            },
            retcol="event_id",
            desc="get_ban_event_ids_in_room",
        )


class RoomMemberBackgroundUpdateStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)
        self.db_pool.updates.register_background_update_handler(
            _MEMBERSHIP_PROFILE_UPDATE_NAME, self._background_add_membership_profile
        )
        self.db_pool.updates.register_background_update_handler(
            _CURRENT_STATE_MEMBERSHIP_UPDATE_NAME,
            self._background_current_state_membership,
        )
        self.db_pool.updates.register_background_index_update(
            "room_membership_forgotten_idx",
            index_name="room_memberships_user_room_forgotten",
            table="room_memberships",
            columns=["user_id", "room_id"],
            where_clause="forgotten = 1",
        )
        self.db_pool.updates.register_background_index_update(
            "room_membership_user_room_index",
            index_name="room_membership_user_room_idx",
            table="room_memberships",
            columns=["user_id", "room_id"],
        )

    async def _background_add_membership_profile(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        target_min_stream_id = progress.get(
            "target_min_stream_id_inclusive",
            self._min_stream_order_on_start,  # type: ignore[attr-defined]
        )
        max_stream_id = progress.get(
            "max_stream_id_exclusive",
            self._stream_order_on_start + 1,  # type: ignore[attr-defined]
        )

        def add_membership_profile_txn(txn: LoggingTransaction) -> int:
            sql = """
                SELECT stream_ordering, event_id, events.room_id, event_json.json
                FROM events
                INNER JOIN event_json USING (event_id)
                WHERE ? <= stream_ordering AND stream_ordering < ?
                AND type = 'm.room.member'
                ORDER BY stream_ordering DESC
                LIMIT ?
            """

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]

            to_update = []
            for _, event_id, room_id, json in rows:
                try:
                    event_json = db_to_json(json)
                    content = event_json["content"]
                except Exception:
                    continue

                display_name = content.get("displayname", None)
                avatar_url = content.get("avatar_url", None)

                if display_name or avatar_url:
                    to_update.append((display_name, avatar_url, event_id, room_id))

            to_update_sql = """
                UPDATE room_memberships SET display_name = ?, avatar_url = ?
                WHERE event_id = ? AND room_id = ?
            """
            txn.execute_batch(to_update_sql, to_update)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, _MEMBERSHIP_PROFILE_UPDATE_NAME, progress
            )

            return len(rows)

        result = await self.db_pool.runInteraction(
            _MEMBERSHIP_PROFILE_UPDATE_NAME, add_membership_profile_txn
        )

        if not result:
            await self.db_pool.updates._end_background_update(
                _MEMBERSHIP_PROFILE_UPDATE_NAME
            )

        return result

    async def _background_current_state_membership(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Update the new membership column on current_state_events.

        This works by iterating over all rooms in alphebetical order.
        """

        def _background_current_state_membership_txn(
            txn: LoggingTransaction, last_processed_room: str
        ) -> tuple[int, bool]:
            processed = 0
            while processed < batch_size:
                txn.execute(
                    """
                        SELECT MIN(room_id) FROM current_state_events WHERE room_id > ?
                    """,
                    (last_processed_room,),
                )
                row = txn.fetchone()
                if not row or not row[0]:
                    return processed, True

                (next_room,) = row

                sql = """
                    UPDATE current_state_events
                    SET membership = (
                        SELECT membership FROM room_memberships
                        WHERE event_id = current_state_events.event_id
                    )
                    WHERE room_id = ?
                """
                txn.execute(sql, (next_room,))
                processed += txn.rowcount

                last_processed_room = next_room

            self.db_pool.updates._background_update_progress_txn(
                txn,
                _CURRENT_STATE_MEMBERSHIP_UPDATE_NAME,
                {"last_processed_room": last_processed_room},
            )

            return processed, False

        # If we haven't got a last processed room then just use the empty
        # string, which will compare before all room IDs correctly.
        last_processed_room = progress.get("last_processed_room", "")

        row_count, finished = await self.db_pool.runInteraction(
            "_background_current_state_membership_update",
            _background_current_state_membership_txn,
            last_processed_room,
        )

        if finished:
            await self.db_pool.updates._end_background_update(
                _CURRENT_STATE_MEMBERSHIP_UPDATE_NAME
            )

        return row_count


class RoomMemberStore(
    RoomMemberWorkerStore,
    RoomMemberBackgroundUpdateStore,
    CacheInvalidationWorkerStore,
):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)


def extract_heroes_from_room_summary(
    details: Mapping[str, MemberSummary], me: str
) -> list[str]:
    """Determine the users that represent a room, from the perspective of the `me` user.

    This function expects `MemberSummary.members` to already be sorted by
    `stream_ordering` like the results from `get_room_summary(...)`.

    The rules which say which users we select are specified in the "Room Summary"
    section of
    https://spec.matrix.org/v1.4/client-server-api/#get_matrixclientv3sync


    Args:
        details: Mapping from membership type to member summary. We expect
            `MemberSummary.members` to already be sorted by `stream_ordering`.
        me: The user for whom we are determining the heroes for.

    Returns a list (possibly empty) of heroes' mxids.
    """
    empty_ms = MemberSummary([], 0)

    joined_user_ids = [
        r[0] for r in details.get(Membership.JOIN, empty_ms).members if r[0] != me
    ]
    invited_user_ids = [
        r[0] for r in details.get(Membership.INVITE, empty_ms).members if r[0] != me
    ]
    gone_user_ids = [
        r[0] for r in details.get(Membership.LEAVE, empty_ms).members if r[0] != me
    ] + [r[0] for r in details.get(Membership.BAN, empty_ms).members if r[0] != me]

    # We expect `MemberSummary.members` to already be sorted by `stream_ordering`
    if joined_user_ids or invited_user_ids:
        return (joined_user_ids + invited_user_ids)[0:5]
    else:
        return gone_user_ids[0:5]


@attr.s(slots=True, auto_attribs=True)
class _JoinedHostsCache:
    """The cached data used by the `_get_joined_hosts_cache`."""

    # Dict of host to the set of their users in the room at the state group.
    hosts_to_joined_users: dict[str, set[str]] = attr.Factory(dict)

    # The state group `hosts_to_joined_users` is derived from. Will be an object
    # if the instance is newly created or if the state is not based on a state
    # group. (An object is used as a sentinel value to ensure that it never is
    # equal to anything else).
    state_group: object | int = attr.Factory(object)

    def __len__(self) -> int:
        return sum(len(v) for v in self.hosts_to_joined_users.values())
