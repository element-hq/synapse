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
"""Persisting MSC4430 member key rooms: the room's own tables keep the member
keys, while everything keyed by user ID (`local_current_membership`, the sliding
sync snapshots, `event_txn_id_device_id`, the per-user caches and the membership
stream cache) gets the user behind the key.

The events are hand built and signed (see `tests.test_utils.member_key_rooms`)
and pushed straight through the persistence controller, as nothing else in the
server can produce them yet. Every unadapted site on these paths parses a member
key as a user ID and raises `MemberKeyHasNoDomainError`, so a regression shows
up as a 500 rather than a wrong answer.
"""

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, Membership
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.rest import admin
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.member_key_rooms import (
    Member,
    MemberKeyRoom,
    register_member_key_room_version,
)
from tests.unittest import override_config

OTHER_ROOM = "!oF3b2QiDEjKZbBYbuSVYn_9ygzXlDzYFmzAZeDTAI7U"


class MemberKeyPersistenceTestCase(unittest.HomeserverTestCase):
    servlets = [admin.register_servlets]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enabled unless a test's `override_config` says otherwise.
        config.setdefault("experimental_features", {}).setdefault(
            "msc4430_enabled", True
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # The room version is not registered by the server yet; persisting into a
        # room of that version needs it to be. Process global, see the helper.
        register_member_key_room_version()

        self.store = hs.get_datastores().main
        persistence = hs.get_storage_controllers().persistence
        assert persistence is not None
        self.persistence = persistence

        self.alice = self.register_user("alice", "pass")
        self.bob = self.register_user("bob", "pass")

        # Alice creates the room with her member key, and joins.
        self.alice_member = Member()
        self.room = MemberKeyRoom(hs, self.alice_member)
        self.get_success(self.room.create())
        self.get_success(
            self.store.create_local_member_key(
                self.room.room_id, self.alice, self.alice_member.signing_key
            )
        )
        self.room_id = self.room.room_id

    def _local_key(self, user_id: str) -> Member:
        """Create a member key of ours for `user_id` in the room."""
        member = Member()
        self.get_success(
            self.store.create_local_member_key(
                self.room_id, user_id, member.signing_key
            )
        )
        return member

    def _local_current_membership(self) -> dict[str, tuple[str, str]]:
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                table="local_current_membership",
                keyvalues={"room_id": self.room_id},
                retcols=("user_id", "membership", "event_id"),
            )
        )
        return {
            user_id: (membership, event_id) for user_id, membership, event_id in rows
        }

    def _membership_snapshots(self) -> dict[str, tuple[str, str]]:
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                table="sliding_sync_membership_snapshots",
                keyvalues={"room_id": self.room_id},
                retcols=("user_id", "membership", "sender"),
            )
        )
        return {user_id: (membership, sender) for user_id, membership, sender in rows}

    def test_local_join_is_keyed_by_user(self) -> None:
        """A join under one of our keys lands in the room's tables under the key,
        and in the user-keyed tables under the user."""
        join = self.get_success(self.room.send_member(self.alice_member, "join"))

        # The room knows the key.
        self.assertEqual(
            self.get_success(self.store.get_users_in_room(self.room_id)),
            [self.alice_member.key],
        )
        self.assertEqual(
            self.get_success(
                self.store.get_partial_current_state_ids(self.room_id)
            ).get((EventTypes.Member, self.alice_member.key)),
            join.event_id,
        )

        # Everything keyed by user ID knows the user.
        self.assertEqual(
            self._local_current_membership(),
            {self.alice: (Membership.JOIN, join.event_id)},
        )
        self.assertEqual(
            self.get_success(self.store.get_local_users_in_room(self.room_id)),
            [self.alice],
        )
        self.assertEqual(
            self.get_success(
                self.store.get_local_current_membership_for_user_in_room(
                    self.alice, self.room_id
                )
            ),
            (Membership.JOIN, join.event_id),
        )
        self.assertEqual(
            self._membership_snapshots(),
            {self.alice: (Membership.JOIN, self.alice_member.key)},
        )
        rooms_for_user = self.get_success(
            self.store.get_rooms_for_local_user_where_membership_is(
                self.alice, [Membership.JOIN]
            )
        )
        self.assertEqual([r.room_id for r in rooms_for_user], [self.room_id])

    def test_get_rooms_for_user(self) -> None:
        """`get_rooms_for_user` (and the batched form) see member key rooms for
        our own users; the key itself is not a user and sees nothing."""
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)), frozenset()
        )
        self.get_success(self.room.send_member(self.alice_member, "join"))

        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)),
            frozenset({self.room_id}),
        )
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_users([self.alice, self.bob])),
            {self.alice: frozenset({self.room_id}), self.bob: frozenset()},
        )
        # The legacy query, by state key, still answers for the key itself.
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice_member.key)),
            frozenset({self.room_id}),
        )

    @override_config({"experimental_features": {"msc4430_enabled": False}})
    def test_flag_off_takes_the_legacy_path(self) -> None:
        """With MSC4430 off, a member-key-shaped state key is as malformed as it
        always was: nothing is mapped and the legacy queries run unchanged."""
        self.get_success(self.room.send_member(self.alice_member, "join"))

        self.assertEqual(self._local_current_membership(), {})
        self.assertEqual(self._membership_snapshots(), {})
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)), frozenset()
        )
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_users([self.alice])),
            {self.alice: frozenset()},
        )

    def test_membership_changes_for_user(self) -> None:
        """`get_membership_changes_for_user` returns the user's membership events
        in member key rooms, and the membership stream cache is bumped for the
        user so that the query is not skipped."""
        before = self.store.get_room_max_token()
        join = self.get_success(self.room.send_member(self.alice_member, "join"))
        after = self.store.get_room_max_token()

        self.assertTrue(
            self.store._membership_stream_cache.has_entity_changed(
                self.alice, before.stream
            )
        )
        # Recorded at the join's own position, as for a user ID state key.
        self.assertEqual(
            self.store._membership_stream_cache._entity_to_key[self.alice],
            join.internal_metadata.stream_ordering,
        )
        changes = self.get_success(
            self.store.get_membership_changes_for_user(self.alice, before, after)
        )
        self.assertEqual([e.event_id for e in changes], [join.event_id])

        # Bob has no key in the room and sees nothing.
        self.assertEqual(
            self.get_success(
                self.store.get_membership_changes_for_user(self.bob, before, after)
            ),
            [],
        )

    def test_membership_change_in_a_batch_is_recorded_at_its_own_position(
        self,
    ) -> None:
        """When a membership event is persisted in a batch after another event,
        the user's change is recorded at the membership event's position, not
        the batch's lowest one (which a client may already hold as a token)."""
        self.get_success(self.room.send_member(self.alice_member, "join"))
        bob_member = self._local_key(self.bob)
        message = self.room.build_message_at_depth(self.alice_member, depth=10)
        join = self.room.build_member(bob_member, "join", depth=10)
        self.get_success(self.room.persist_batch([message, join]))

        assert message.internal_metadata.stream_ordering is not None
        assert join.internal_metadata.stream_ordering is not None
        self.assertLess(
            message.internal_metadata.stream_ordering,
            join.internal_metadata.stream_ordering,
        )
        self.assertEqual(
            self.store._membership_stream_cache._entity_to_key[self.bob],
            join.internal_metadata.stream_ordering,
        )
        # A token minted between the two events still sees the join.
        self.assertTrue(
            self.store._membership_stream_cache.has_entity_changed(
                self.bob, message.internal_metadata.stream_ordering
            )
        )

    def test_backfilled_membership_is_not_current(self) -> None:
        """A backfilled membership event does not touch the current local
        membership, as for a user ID state key."""
        self.get_success(self.room.send_member(self.alice_member, "join"))
        bob_member = self._local_key(self.bob)
        old_join = self.room.build_member(bob_member, "join", depth=2)
        self.get_success(self.room.persist(old_join, backfilled=True))

        self.assertEqual(set(self._local_current_membership()), {self.alice})
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.bob)), frozenset()
        )

    def test_leave_removes_membership(self) -> None:
        """A leave updates the user's row, and when the leaver was our last
        member the server leaves the room (which depends on recognising the
        leaving key as ours)."""
        self.get_success(self.room.send_member(self.alice_member, "join"))
        leave = self.get_success(self.room.send_member(self.alice_member, "leave"))

        self.assertEqual(
            self._local_current_membership(),
            {self.alice: (Membership.LEAVE, leave.event_id)},
        )
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)), frozenset()
        )
        self.assertEqual(
            self._membership_snapshots(),
            {self.alice: (Membership.LEAVE, self.alice_member.key)},
        )
        left = self.get_success(
            self.store.get_rooms_for_local_user_where_membership_is(
                self.alice, [Membership.LEAVE]
            )
        )
        self.assertEqual([r.room_id for r in left], [self.room_id])
        # Nobody of ours is left, so the server has left the room and the
        # current state was cleared.
        self.assertEqual(
            self.get_success(self.store.get_users_in_room(self.room_id)), []
        )
        self.assertEqual(
            self.get_success(self.store.get_partial_current_state_ids(self.room_id)),
            {},
        )

    def test_leave_with_other_local_member_keeps_the_room(self) -> None:
        """When another of our users remains, the server stays in the room; that
        check consults `local_current_membership` by user ID."""
        bob_member = self._local_key(self.bob)
        self.get_success(self.room.send_member(self.alice_member, "join"))
        self.get_success(self.room.send_member(bob_member, "join"))
        self.get_success(self.room.send_member(self.alice_member, "leave"))

        self.assertEqual(
            self.get_success(self.store.get_users_in_room(self.room_id)),
            [bob_member.key],
        )
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.bob)),
            frozenset({self.room_id}),
        )
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.alice)), frozenset()
        )

    def test_key_from_another_room_maps_to_nothing(self) -> None:
        """A key we hold for Bob in another room, appearing in this room's
        events, is somebody else here: Bob gets no membership from it."""
        stranger = Member()
        self.get_success(
            self.store.create_local_member_key(
                OTHER_ROOM, self.bob, stranger.signing_key
            )
        )
        self.get_success(self.room.send_member(self.alice_member, "join"))
        before = self.store.get_room_max_token()
        self.get_success(self.room.send_member(stranger, "join"))
        after = self.store.get_room_max_token()

        self.assertEqual(
            set(self.get_success(self.store.get_users_in_room(self.room_id))),
            {self.alice_member.key, stranger.key},
        )
        self.assertEqual(set(self._local_current_membership()), {self.alice})
        self.assertEqual(set(self._membership_snapshots()), {self.alice})
        self.assertEqual(
            self.get_success(self.store.get_rooms_for_user(self.bob)), frozenset()
        )
        # Nor is it a membership change of Bob's (the stream cache is bumped for
        # the key, which is not a user, and the query is scoped to the room).
        self.store._membership_stream_cache.entity_has_changed(self.bob, after.stream)
        self.assertEqual(
            self.get_success(
                self.store.get_membership_changes_for_user(self.bob, before, after)
            ),
            [],
        )

    def test_out_of_band_invite(self) -> None:
        """A remote invite for one of our users is an outlier that never reaches
        the current state; its membership is recorded by user ID, and the
        user-keyed invite lookup sees it."""
        bob_member = self._local_key(self.bob)
        inviter = Member()
        self.assertEqual(
            self.get_success(self.store.get_invited_rooms_for_local_user(self.bob)), []
        )

        invite = self.get_success(
            self.room.send_out_of_band_member(
                bob_member,
                "invite",
                sender=inviter,
                unsigned={
                    "invite_room_state": [
                        {
                            "type": EventTypes.Name,
                            "state_key": "",
                            "sender": inviter.key,
                            "content": {"name": "Invited"},
                        }
                    ]
                },
            )
        )

        self.assertEqual(
            self._local_current_membership(),
            {self.bob: (Membership.INVITE, invite.event_id)},
        )
        self.assertEqual(
            self._membership_snapshots(), {self.bob: (Membership.INVITE, inviter.key)}
        )
        invited = self.get_success(
            self.store.get_invited_rooms_for_local_user(self.bob)
        )
        self.assertEqual(
            [(r.room_id, r.sender, r.event_id) for r in invited],
            [(self.room_id, inviter.key, invite.event_id)],
        )
        # The current state is untouched by an outlier.
        self.assertEqual(
            self.get_success(self.store.get_users_in_room(self.room_id)), []
        )

        # Rejecting it (another out-of-band event) updates the same row.
        leave = self.get_success(
            self.room.send_out_of_band_member(bob_member, "leave", sender=bob_member)
        )
        self.assertEqual(
            self._local_current_membership(),
            {self.bob: (Membership.LEAVE, leave.event_id)},
        )
        self.assertEqual(
            self.get_success(self.store.get_invited_rooms_for_local_user(self.bob)), []
        )

    def test_transaction_id_is_keyed_by_user(self) -> None:
        """`event_txn_id_device_id` has a foreign key to `devices`, so the sender
        of a locally sent event is stored as the user, not the key."""
        self.get_success(self.room.send_member(self.alice_member, "join"))
        self.get_success(self.store.store_device(self.alice, "DEV", None))

        message = self.get_success(
            self.room.send_message(
                self.alice_member,
                internal_metadata={"txn_id": "txn1", "device_id": "DEV"},
            )
        )

        self.assertEqual(
            self.get_success(
                self.store.get_event_id_from_transaction_id_and_device_id(
                    self.room_id, self.alice, "DEV", "txn1"
                )
            ),
            message.event_id,
        )
        rows = self.get_success(
            self.store.db_pool.simple_select_list(
                table="event_txn_id_device_id",
                keyvalues={"event_id": message.event_id},
                retcols=("user_id",),
            )
        )
        self.assertEqual(rows, [(self.alice,)])

        # Persisting again under the same transaction ID is deduplicated to the
        # first event (the lookup is by user, as the row is).
        duplicate = self.room.build_message_at_depth(self.alice_member, depth=10)
        duplicate.internal_metadata.txn_id = "txn1"
        duplicate.internal_metadata.device_id = "DEV"
        persisted = self.get_success(self.room.persist_batch([duplicate]))
        self.assertEqual([e.event_id for e in persisted], [message.event_id])

    def _prune(self, new_event: EventBase, extremities: list[EventBase]) -> set[str]:
        """Run extremity pruning as if `new_event` were being persisted with the
        given (persisted) events as the room's other forward extremities, each
        with its own state group."""
        new_latest_event_ids = {e.event_id for e in extremities} | {new_event.event_id}
        event_id_to_state_group = {e.event_id: i for i, e in enumerate(extremities)}
        event_id_to_state_group[new_event.event_id] = len(extremities)
        return self.get_success(
            self.persistence._prune_extremities(
                self.room_id,
                new_latest_event_ids,
                len(extremities),
                event_id_to_state_group,
                [
                    (
                        new_event,
                        EventContext.for_outlier(self.hs.get_storage_controllers()),
                    )
                ],
            )
        )

    def test_prune_extremities_keeps_our_members_events(self) -> None:
        """An old extremity sent by one of our users' keys is never dropped, as
        for any of our own events."""
        bob_member = self._local_key(self.bob)
        self.get_success(self.room.send_member(self.alice_member, "join"))
        self.get_success(self.room.send_member(bob_member, "join"))
        stuck = self.get_success(
            self.room.send_message(bob_member, origin_server_ts=1, depth=3)
        )
        new = self.room.build_message_at_depth(self.alice_member, depth=200)
        self.reactor.advance(2 * 24 * 60 * 60)

        self.assertEqual(self._prune(new, [stuck]), {stuck.event_id, new.event_id})

    def test_prune_extremities_drops_old_remote_extremity(self) -> None:
        """An old extremity from a key we do not hold is dropped once it is a day
        old and a hundred events behind; the senders of the new events (member
        keys, which name no server) must not be parsed as user IDs."""
        self.get_success(self.room.send_member(self.alice_member, "join"))
        stranger = Member()
        self.get_success(self.room.send_member(stranger, "join"))
        stuck = self.get_success(
            self.room.send_message(stranger, origin_server_ts=1, depth=4)
        )
        new = self.room.build_message_at_depth(self.alice_member, depth=200)
        # "Old" is relative to the clock, which starts near zero in tests.
        self.reactor.advance(2 * 24 * 60 * 60)

        self.assertEqual(self._prune(new, [stuck]), {new.event_id})

    def test_prune_extremities_same_sender_is_dropped_sooner(self) -> None:
        """A recent extremity from the same origin as a new event is dropped
        after twenty events; in a member key room the origin is the key."""
        self.get_success(self.room.send_member(self.alice_member, "join"))
        stranger = Member()
        self.get_success(self.room.send_member(stranger, "join"))
        recent = self.get_success(self.room.send_message(stranger, depth=4))
        other_sender = self.room.build_message_at_depth(self.alice_member, depth=40)
        same_sender = self.room.build_message_at_depth(stranger, depth=40)

        # Too new to drop for a different sender, dropped for the same one.
        self.assertEqual(
            self._prune(other_sender, [recent]),
            {recent.event_id, other_sender.event_id},
        )
        self.assertEqual(self._prune(same_sender, [recent]), {same_sender.event_id})


class InvalidateStateCachesTestCase(unittest.HomeserverTestCase):
    """`_invalidate_state_caches` with member keys among the changed members:
    the per-host caches cannot be invalidated by host (a key names none), so the
    whole room's entries go; a user ID in the same batch still invalidates just
    its host."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def _prefill_host_caches(self, room_id: str, hosts: list[str]) -> None:
        for host in hosts:
            self.store.is_host_joined.cache.prefill((room_id, host), False)
            self.store.is_host_invited.cache.prefill((room_id, host), False)

    def _cached_hosts(self, room_id: str, hosts: list[str]) -> set[str]:
        return {
            host
            for host in hosts
            if self.store.is_host_joined.cache.get_immediate((room_id, host), None)
            is not None
            and self.store.is_host_invited.cache.get_immediate((room_id, host), None)
            is not None
        }

    def test_user_id_invalidates_its_host_only(self) -> None:
        hosts = ["a.example", "b.example"]
        self._prefill_host_caches("!r:test", hosts)
        self._prefill_host_caches("!other:test", hosts)

        self.store._invalidate_state_caches("!r:test", {"@u:a.example"})

        self.assertEqual(self._cached_hosts("!r:test", hosts), {"b.example"})
        self.assertEqual(self._cached_hosts("!other:test", hosts), set(hosts))

    def test_member_key_invalidates_every_host_in_the_room(self) -> None:
        hosts = ["a.example", "b.example"]
        self._prefill_host_caches("!r:test", hosts)
        self._prefill_host_caches("!other:test", hosts)

        self.store._invalidate_state_caches("!r:test", {Member().key})

        self.assertEqual(self._cached_hosts("!r:test", hosts), set())
        self.assertEqual(self._cached_hosts("!other:test", hosts), set(hosts))
