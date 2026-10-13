#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
"""Storage for MSC4430 member keys: the keys we hold for our own users
(`msc4430_member_keys`) and what we know about everyone else's
(`msc4430_member_links`).

Member keys are per room. Every lookup by key takes the room it was seen in, so
that a key of ours from one room appearing in another room's events does not
resolve to anything there.

This store is mixed into `RoomMemberWorkerStore` so that the membership queries
which will need to translate member keys can use it directly, on workers too.

It also owns the *local identity map* (`get_local_identity_map_txn` and
`get_local_identity_map`): the one way storage code turns the identities a
room's events name (user IDs, or member keys in a member key room) into the
local users behind them, for the tables and caches that are keyed by user ID.
Storage cannot import the handler that owns identity resolution for the rest of
the server (`synapse.handlers.member_keys`), so the storage half lives here.
"""

import logging
from typing import TYPE_CHECKING, Collection, Iterable, Mapping, cast

from signedjson.types import SigningKey

from synapse.api.constants import MemberKeys
from synapse.storage._base import make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
)
from synapse.storage.databases.main.cache import CacheInvalidationWorkerStore
from synapse.storage.member_keys import (
    LocalMemberKey,
    MemberLink,
    decode_member_signing_key,
    encode_member_signing_key,
)
from synapse.types import MemberKey, get_domain_from_id, is_member_key
from synapse.util.caches.descriptors import cached, cachedList

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class MemberKeysWorkerStore(CacheInvalidationWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)
        self._msc4430_enabled = hs.config.experimental.msc4430_enabled
        self._is_mine_id = hs.is_mine_id

    # ------------------------------------------------------------------
    # The keys we hold
    # ------------------------------------------------------------------

    @cached(max_entries=100000)
    async def get_local_member_key(
        self, room_id: str, user_id: str
    ) -> LocalMemberKey | None:
        """The key we hold for the given local user in the given room, if any."""
        row = cast(
            tuple[str, str] | None,
            await self.db_pool.simple_select_one(
                table="msc4430_member_keys",
                keyvalues={"room_id": room_id, "user_id": user_id},
                retcols=("member_key", "signing_key"),
                allow_none=True,
                desc="get_local_member_key",
            ),
        )
        if row is None:
            return None
        return LocalMemberKey(
            room_id=room_id,
            user_id=user_id,
            member_key=row[0],
            signing_key=decode_member_signing_key(row[1]),
        )

    @cached(max_entries=100000)
    async def get_local_member_key_by_key(
        self, room_id: str, member_key: str
    ) -> LocalMemberKey | None:
        """The key we hold with the given public form, if it is ours *in this
        room*. A key of ours seen in another room is not ours there."""
        row = cast(
            tuple[str, str] | None,
            await self.db_pool.simple_select_one(
                table="msc4430_member_keys",
                keyvalues={"room_id": room_id, "member_key": member_key},
                retcols=("user_id", "signing_key"),
                allow_none=True,
                desc="get_local_member_key_by_key",
            ),
        )
        if row is None:
            return None
        return LocalMemberKey(
            room_id=room_id,
            user_id=row[0],
            member_key=member_key,
            signing_key=decode_member_signing_key(row[1]),
        )

    @cachedList(
        cached_method_name="get_local_member_key_by_key", list_name="member_keys"
    )
    async def get_local_member_keys_by_key(
        self, room_id: str, member_keys: Collection[str]
    ) -> Mapping[str, LocalMemberKey | None]:
        """Batched `get_local_member_key_by_key` for one room."""
        rows = cast(
            list[tuple[str, str, str]],
            await self.db_pool.simple_select_many_batch(
                table="msc4430_member_keys",
                column="member_key",
                iterable=member_keys,
                keyvalues={"room_id": room_id},
                retcols=("member_key", "user_id", "signing_key"),
                desc="get_local_member_keys_by_key",
            ),
        )
        result: dict[str, LocalMemberKey | None] = dict.fromkeys(member_keys)
        for member_key, user_id, signing_key in rows:
            result[member_key] = LocalMemberKey(
                room_id=room_id,
                user_id=user_id,
                member_key=member_key,
                signing_key=decode_member_signing_key(signing_key),
            )
        return result

    def get_local_user_ids_for_member_keys_txn(
        self, txn: LoggingTransaction, room_id: str, member_keys: Collection[str]
    ) -> dict[str, str]:
        """Map the given member keys to the local users they belong to in the given
        room. Keys that are not ours in that room are left out.

        For use inside event persistence transactions; see
        `get_local_member_keys_by_key` otherwise.
        """
        if not member_keys:
            return {}
        clause, args = make_in_list_sql_clause(
            self.database_engine, "member_key", member_keys
        )
        txn.execute(
            f"SELECT member_key, user_id FROM msc4430_member_keys"
            f" WHERE room_id = ? AND {clause}",
            [room_id, *args],
        )
        return dict(cast(Iterable[tuple[str, str]], txn))

    # ------------------------------------------------------------------
    # The local identity map
    # ------------------------------------------------------------------

    def _partition_identities(
        self, identities: Iterable[str]
    ) -> tuple[dict[str, str], list[str]]:
        """Split room member identities into the local user IDs (mapped to
        themselves) and the member keys still to be looked up. Everything else,
        other servers' user IDs included, is dropped.

        Member keys are only looked up when MSC4430 is enabled: with it off there
        are no member key rooms, and a member-key-shaped state key in some other
        room is as malformed as it always was.
        """
        local_users: dict[str, str] = {}
        member_keys: list[str] = []
        for identity in identities:
            if is_member_key(identity):
                if self._msc4430_enabled:
                    member_keys.append(identity)
            elif self._is_mine_id(identity):
                local_users[identity] = identity
        return local_users, member_keys

    def get_local_identity_map_txn(
        self, txn: LoggingTransaction, room_id: str, identities: Iterable[str]
    ) -> dict[str, str]:
        """Map room member identities appearing in `room_id` to the local users
        behind them: our users' IDs map to themselves, and member keys we hold in
        that room map to their user. Identities that are not ours (other servers'
        user IDs, other members' keys, and our own keys for *other* rooms) are left
        out.

        This is the member-key aware form of filtering identities with
        `is_mine_id`, for the tables and caches that are keyed by user ID
        (`local_current_membership`, the sliding sync snapshots,
        `event_txn_id_device_id`, `get_rooms_for_user` and friends). One batched
        query, and none at all when no identity is a member key, so rooms without
        member keys cost nothing extra.
        """
        local_users, member_keys = self._partition_identities(identities)
        if member_keys:
            local_users.update(
                self.get_local_user_ids_for_member_keys_txn(txn, room_id, member_keys)
            )
        return local_users

    async def get_local_identity_map(
        self, room_id: str, identities: Iterable[str]
    ) -> dict[str, str]:
        """`get_local_identity_map_txn` outside a transaction, from the cached
        per-key lookups. Keys never change once created, so a cached answer can
        only ever be missing (and `create_local_member_key` invalidates), never
        wrong."""
        local_users, member_keys = self._partition_identities(identities)
        if member_keys:
            keys = await self.get_local_member_keys_by_key(room_id, member_keys)
            for member_key, local_key in keys.items():
                if local_key is not None:
                    local_users[member_key] = local_key.user_id
        return local_users

    async def get_user_ids_for_identities(
        self, room_id: str, identities: Iterable[str]
    ) -> dict[str, str | None]:
        """The user behind each room member identity: a user ID is its own; a
        member key we hold in the room maps to our user; any other member key
        maps to the user its link claims if that link is *verified*, else to
        `None` (an unverified claim is anyone's to make, so such a key is
        nobody's). Member keys map to `None` when MSC4430 is off.

        Rooms without member keys query nothing.
        """
        users: dict[str, str | None] = {}
        member_keys: list[str] = []
        for identity in identities:
            if is_member_key(identity):
                users[identity] = None
                if self._msc4430_enabled:
                    member_keys.append(identity)
            else:
                users[identity] = identity
        if not member_keys:
            return users

        local_keys = await self.get_local_member_keys_by_key(room_id, member_keys)
        remote_keys = []
        for member_key in member_keys:
            local_key = local_keys.get(member_key)
            if local_key is not None:
                users[member_key] = local_key.user_id
            else:
                remote_keys.append(member_key)
        if remote_keys:
            links = await self.get_member_links(room_id, remote_keys)
            for member_key, link in links.items():
                if link is not None:
                    users[member_key] = link.verified_user_id
        return users

    async def get_hosts_for_identities(
        self, room_id: str, identities: Iterable[str]
    ) -> dict[str, str]:
        """The server behind each room member identity, for routing: the domain
        of a user ID, our own name for a member key we hold in the room, and the
        *claimed* domain of any other member key we have a link for (verified or
        not: an unverified claim is enough to route to, and grants nothing).
        Identities whose server we do not know are left out, as are member keys
        when MSC4430 is off.

        The member-key aware form of taking `get_domain_from_id` of every
        member; rooms without member keys query nothing.
        """
        hosts: dict[str, str] = {}
        member_keys: list[str] = []
        for identity in identities:
            if is_member_key(identity):
                if self._msc4430_enabled:
                    member_keys.append(identity)
            else:
                hosts[identity] = get_domain_from_id(identity)
        if not member_keys:
            return hosts

        local_keys = await self.get_local_member_keys_by_key(room_id, member_keys)
        remote_keys = []
        for member_key in member_keys:
            if local_keys.get(member_key) is not None:
                hosts[member_key] = self.server_name
            else:
                remote_keys.append(member_key)
        if remote_keys:
            links = await self.get_member_links(room_id, remote_keys)
            for member_key, link in links.items():
                if link is not None:
                    hosts[member_key] = link.domain
        return hosts

    @cached(max_entries=100000)
    async def get_local_member_keys_for_user(self, user_id: str) -> Mapping[str, str]:
        """All the keys we hold for a local user, as a map from room ID to member
        key."""
        rows = cast(
            list[tuple[str, str]],
            await self.db_pool.simple_select_list(
                table="msc4430_member_keys",
                keyvalues={"user_id": user_id},
                retcols=("room_id", "member_key"),
                desc="get_local_member_keys_for_user",
            ),
        )
        return dict(rows)

    @cached(max_entries=10000)
    async def get_local_member_keys_in_room(self, room_id: str) -> Mapping[str, str]:
        """All the keys we hold in a room, as a map from member key to local user ID.
        Includes users who are not, or are no longer, in the room."""
        rows = cast(
            list[tuple[str, str]],
            await self.db_pool.simple_select_list(
                table="msc4430_member_keys",
                keyvalues={"room_id": room_id},
                retcols=("member_key", "user_id"),
                desc="get_local_member_keys_in_room",
            ),
        )
        return dict(rows)

    async def create_local_member_key(
        self, room_id: str, user_id: str, signing_key: SigningKey
    ) -> LocalMemberKey:
        """Store a freshly generated key for a local user in a room, unless one is
        already stored.

        A user's key in a room must never change, so this is insert-if-absent: if a
        key already exists for this (room, user), including one stored by a
        concurrent call, that key is returned and `signing_key` is discarded.

        Args:
            room_id: The room the key identifies the user in.
            user_id: The local user.
            signing_key: The private half; the public form is derived from it.
                Must be freshly generated: public keys are unique across the
                table, so a key already stored for another (room, user) raises
                `IntegrityError` rather than being shared.

        Returns:
            The key now stored for this (room, user).
        """
        if (
            signing_key.alg != MemberKeys.KEY_ALGORITHM
            or signing_key.version != MemberKeys.KEY_VERSION
        ):
            raise ValueError(
                "Member keys have key ID %s, not %s:%s"
                % (MemberKeys.KEY_ID, signing_key.alg, signing_key.version)
            )
        member_key = MemberKey.from_signing_key(signing_key).to_string()

        def create_local_member_key_txn(txn: LoggingTransaction) -> LocalMemberKey:
            inserted = self.db_pool.simple_upsert_txn(
                txn,
                table="msc4430_member_keys",
                keyvalues={"room_id": room_id, "user_id": user_id},
                values={},
                insertion_values={
                    "member_key": member_key,
                    "signing_key": encode_member_signing_key(signing_key),
                    "created_ts": self.clock.time_msec(),
                },
            )
            if not inserted:
                existing = cast(
                    tuple[str, str],
                    self.db_pool.simple_select_one_txn(
                        txn,
                        table="msc4430_member_keys",
                        keyvalues={"room_id": room_id, "user_id": user_id},
                        retcols=("member_key", "signing_key"),
                    ),
                )
                return LocalMemberKey(
                    room_id=room_id,
                    user_id=user_id,
                    member_key=existing[0],
                    signing_key=decode_member_signing_key(existing[1]),
                )

            self._invalidate_cache_and_stream(
                txn, self.get_local_member_key, (room_id, user_id)
            )
            self._invalidate_cache_and_stream(
                txn, self.get_local_member_key_by_key, (room_id, member_key)
            )
            self._invalidate_cache_and_stream(
                txn, self.get_local_member_keys_for_user, (user_id,)
            )
            self._invalidate_cache_and_stream(
                txn, self.get_local_member_keys_in_room, (room_id,)
            )
            return LocalMemberKey(
                room_id=room_id,
                user_id=user_id,
                member_key=member_key,
                signing_key=signing_key,
            )

        return await self.db_pool.runInteraction(
            "create_local_member_key", create_local_member_key_txn
        )

    # ------------------------------------------------------------------
    # Links: what we know about keys we do not hold
    # ------------------------------------------------------------------

    @cached(max_entries=100000)
    async def get_member_link(self, room_id: str, member_key: str) -> MemberLink | None:
        row = cast(
            tuple[str, str | None, bool, str | None, str | None] | None,
            await self.db_pool.simple_select_one(
                table="msc4430_member_links",
                keyvalues={"room_id": room_id, "member_key": member_key},
                retcols=("domain", "localpart", "verified", "event_id", "key_id"),
                allow_none=True,
                desc="get_member_link",
            ),
        )
        if row is None:
            return None
        return MemberLink(
            room_id=room_id,
            member_key=member_key,
            domain=row[0],
            localpart=row[1],
            verified=bool(row[2]),
            event_id=row[3],
            key_id=row[4],
        )

    def get_member_link_txn(
        self, txn: LoggingTransaction, room_id: str, member_key: str
    ) -> MemberLink | None:
        """Transaction variant of `get_member_link`, so that a decision about a
        link (whether an event may re-point it, whether a failed check may
        downgrade it) can be made and written in one transaction with
        `upsert_member_link_txn`."""
        row = cast(
            tuple[str, str | None, bool, str | None, str | None] | None,
            self.db_pool.simple_select_one_txn(
                txn,
                table="msc4430_member_links",
                keyvalues={"room_id": room_id, "member_key": member_key},
                retcols=("domain", "localpart", "verified", "event_id", "key_id"),
                allow_none=True,
            ),
        )
        if row is None:
            return None
        return MemberLink(
            room_id=room_id,
            member_key=member_key,
            domain=row[0],
            localpart=row[1],
            verified=bool(row[2]),
            event_id=row[3],
            key_id=row[4],
        )

    @cachedList(cached_method_name="get_member_link", list_name="member_keys")
    async def get_member_links(
        self, room_id: str, member_keys: Collection[str]
    ) -> Mapping[str, MemberLink | None]:
        """Batched `get_member_link` for one room."""
        rows = cast(
            list[tuple[str, str, str | None, bool, str | None, str | None]],
            await self.db_pool.simple_select_many_batch(
                table="msc4430_member_links",
                column="member_key",
                iterable=member_keys,
                keyvalues={"room_id": room_id},
                retcols=(
                    "member_key",
                    "domain",
                    "localpart",
                    "verified",
                    "event_id",
                    "key_id",
                ),
                desc="get_member_links",
            ),
        )
        result: dict[str, MemberLink | None] = dict.fromkeys(member_keys)
        for member_key, domain, localpart, verified, event_id, key_id in rows:
            result[member_key] = MemberLink(
                room_id=room_id,
                member_key=member_key,
                domain=domain,
                localpart=localpart,
                verified=bool(verified),
                event_id=event_id,
                key_id=key_id,
            )
        return result

    async def get_member_links_in_room(self, room_id: str) -> Mapping[str, MemberLink]:
        """Every link we have in a room, keyed by member key. The keys we hold
        ourselves have no link; see `get_local_member_keys_in_room` for those."""
        rows = cast(
            list[tuple[str, str, str | None, bool, str | None, str | None]],
            await self.db_pool.simple_select_list(
                table="msc4430_member_links",
                keyvalues={"room_id": room_id},
                retcols=(
                    "member_key",
                    "domain",
                    "localpart",
                    "verified",
                    "event_id",
                    "key_id",
                ),
                desc="get_member_links_in_room",
            ),
        )
        return {
            member_key: MemberLink(
                room_id=room_id,
                member_key=member_key,
                domain=domain,
                localpart=localpart,
                verified=bool(verified),
                event_id=event_id,
                key_id=key_id,
            )
            for member_key, domain, localpart, verified, event_id, key_id in rows
        }

    async def get_member_links_for_claimed_user(
        self, room_id: str, domain: str, localpart: str
    ) -> list[MemberLink]:
        """The links in a room whose claimed user ID is `@localpart:domain`:
        verified ones first, then by member key.

        There may be several (an unverified claim is anyone's to make, and a user
        who lost their server's database may have a verified key and an older one
        a surviving server key still vouches for), so which key, if any, is "the"
        user's is the caller's decision, never an unverified claimant's.
        """
        rows = cast(
            list[tuple[str, bool, str | None, str | None]],
            await self.db_pool.simple_select_list(
                table="msc4430_member_links",
                keyvalues={
                    "room_id": room_id,
                    "domain": domain,
                    "localpart": localpart,
                },
                retcols=("member_key", "verified", "event_id", "key_id"),
                desc="get_member_links_for_claimed_user",
            ),
        )
        rows.sort(key=lambda row: (not row[1], row[0]))
        return [
            MemberLink(
                room_id=room_id,
                member_key=member_key,
                domain=domain,
                localpart=localpart,
                verified=bool(verified),
                event_id=event_id,
                key_id=key_id,
            )
            for member_key, verified, event_id, key_id in rows
        ]

    async def get_member_keys_for_domain_in_room(
        self, room_id: str, domain: str, verified_only: bool = False
    ) -> frozenset[str]:
        """The member keys in a room that belong to `domain`.

        For our own server name this is the keys we hold in the room: links never
        describe our own keys, and a link claiming our domain for a key we do not
        hold is somebody else's claim. For any other domain it is the keys whose
        links claim it. Pass `verified_only=True` wherever the answer grants
        something to `domain` (serving it room data, treating it as a member
        server): an unverified claim is anyone's to make. Leave it `False` for
        routing.
        """
        if domain == self.server_name:
            return frozenset(await self.get_local_member_keys_in_room(room_id))

        keyvalues: dict[str, object] = {"room_id": room_id, "domain": domain}
        if verified_only:
            keyvalues["verified"] = True
        rows = await self.db_pool.simple_select_onecol(
            table="msc4430_member_links",
            keyvalues=keyvalues,
            retcol="member_key",
            desc="get_member_keys_for_domain_in_room",
        )
        return frozenset(rows)

    async def upsert_member_link(self, link: MemberLink) -> MemberLink | None:
        """Insert or replace the link for `link.member_key` in `link.room_id`.

        Returns:
            The link previously stored, or `None` if there was none, so that the
            caller can tell what changed (the mapped user ID, the routing domain).
        """
        return await self.db_pool.runInteraction(
            "upsert_member_link", self.upsert_member_link_txn, link
        )

    def upsert_member_link_txn(
        self, txn: LoggingTransaction, link: MemberLink
    ) -> MemberLink | None:
        existing = cast(
            tuple[str, str | None, bool, str | None, str | None] | None,
            self.db_pool.simple_select_one_txn(
                txn,
                table="msc4430_member_links",
                keyvalues={"room_id": link.room_id, "member_key": link.member_key},
                retcols=("domain", "localpart", "verified", "event_id", "key_id"),
                allow_none=True,
            ),
        )
        previous: MemberLink | None = None
        if existing is not None:
            previous = MemberLink(
                room_id=link.room_id,
                member_key=link.member_key,
                domain=existing[0],
                localpart=existing[1],
                verified=bool(existing[2]),
                event_id=existing[3],
                key_id=existing[4],
            )

        self.db_pool.simple_upsert_txn(
            txn,
            table="msc4430_member_links",
            keyvalues={"room_id": link.room_id, "member_key": link.member_key},
            values={
                "domain": link.domain,
                "localpart": link.localpart,
                "verified": link.verified,
                "event_id": link.event_id,
                "key_id": link.key_id,
            },
        )
        self._invalidate_cache_and_stream(
            txn, self.get_member_link, (link.room_id, link.member_key)
        )
        return previous

    async def get_verified_link_signing_keys(self) -> list[tuple[str, str]]:
        """The distinct (domain, key ID) pairs of server signing keys that vouch for
        at least one verified link: what a re-verification sweep has to check.

        A key ID alone does not identify a key: a server can publish different
        material under a reused ID (after a compromise, or a domain changing
        hands). Re-verification must compare the current material with what
        signed `event_id`, by re-checking that co-signature.
        """

        def get_verified_link_signing_keys_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[str, str]]:
            txn.execute(
                "SELECT DISTINCT domain, key_id FROM msc4430_member_links"
                " WHERE verified ORDER BY domain, key_id"
            )
            return cast(list[tuple[str, str]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_verified_link_signing_keys", get_verified_link_signing_keys_txn
        )

    async def get_links_for_signing_key(
        self, domain: str, key_id: str, verified: bool | None = None
    ) -> list[MemberLink]:
        """The links whose co-signature named the given server signing key:
        those it currently vouches for (`verified=True`), those it used to (the
        key is no longer current and the links were downgraded, `verified=False`:
        what re-signing after a key rotation would repair), or both (`None`)."""
        keyvalues: dict[str, object] = {"domain": domain, "key_id": key_id}
        if verified is not None:
            keyvalues["verified"] = verified
        rows = cast(
            list[tuple[str, str, str | None, bool, str | None]],
            await self.db_pool.simple_select_list(
                table="msc4430_member_links",
                keyvalues=keyvalues,
                retcols=("room_id", "member_key", "localpart", "verified", "event_id"),
                desc="get_links_for_signing_key",
            ),
        )
        return [
            MemberLink(
                room_id=room_id,
                member_key=member_key,
                domain=domain,
                localpart=localpart,
                verified=bool(row_verified),
                event_id=event_id,
                key_id=key_id,
            )
            for room_id, member_key, localpart, row_verified, event_id in rows
        ]

    async def mark_links_unverified(
        self, domain: str, key_id: str
    ) -> list[tuple[str, str]]:
        """Mark every link vouched for by the given server signing key as
        unverified: the key is no longer that domain's current key. The event
        and key ID are kept, so the links can be found again if the events are
        re-signed.

        Returns:
            The (room ID, member key) pairs that changed.
        """

        def mark_links_unverified_txn(
            txn: LoggingTransaction,
        ) -> list[tuple[str, str]]:
            txn.execute(
                "SELECT room_id, member_key FROM msc4430_member_links"
                " WHERE domain = ? AND key_id = ? AND verified",
                (domain, key_id),
            )
            changed = cast(list[tuple[str, str]], txn.fetchall())
            if not changed:
                return []
            txn.execute(
                "UPDATE msc4430_member_links SET verified = ?"
                " WHERE domain = ? AND key_id = ? AND verified",
                (False, domain, key_id),
            )
            # A key rotation can downgrade every link of a large server at once.
            self._invalidate_cache_and_stream_bulk(txn, self.get_member_link, changed)
            return changed

        return await self.db_pool.runInteraction(
            "mark_links_unverified", mark_links_unverified_txn
        )

    def clear_link_localpart_for_redacted_event_txn(
        self, txn: LoggingTransaction, room_id: str, redacted_event_id: str
    ) -> list[str]:
        """Forget the localpart of every link learned from the given event, which
        has been redacted: those keys then map to no user. The domain (routing)
        and the verification are kept.

        Returns:
            The member keys whose links changed (at most one for a well-formed
            `m.room.member` event; empty if no link was learned from the event).
        """
        member_keys = self.db_pool.simple_select_onecol_txn(
            txn,
            table="msc4430_member_links",
            keyvalues={"room_id": room_id, "event_id": redacted_event_id},
            retcol="member_key",
        )
        if not member_keys:
            return []
        self.db_pool.simple_update_txn(
            txn,
            table="msc4430_member_links",
            keyvalues={"room_id": room_id, "event_id": redacted_event_id},
            updatevalues={"localpart": None},
        )
        for member_key in member_keys:
            self._invalidate_cache_and_stream(
                txn, self.get_member_link, (room_id, member_key)
            )
        return member_keys
