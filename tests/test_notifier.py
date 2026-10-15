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

import logging
from collections import Counter

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.notifier import wait_for_stream_token_timeout_counter
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, MultiWriterStreamToken, StreamKeyType, StreamToken
from synapse.util.clock import Clock
from synapse.util.duration import Duration

import tests.unittest

logger = logging.getLogger(__name__)


class NotifierTestCase(tests.unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.notifier = self.hs.get_notifier()

    def test_wait_for_stream_token_with_caught_up_token(self) -> None:
        """
        Test `wait_for_stream_token` when we receive a token that we are caught up to.
        """
        # Create a token
        receipt_id_gen = self.store.get_receipts_stream_id_gen()
        receipt_token = MultiWriterStreamToken.from_generator(receipt_id_gen)
        token = StreamToken.START.copy_and_replace(StreamKeyType.RECEIPT, receipt_token)

        # Function under test
        wait_d = defer.ensureDeferred(self.notifier.wait_for_stream_token(token))

        # Done waiting and caught-up (True)
        wait_result = self.get_success(wait_d)
        self.assertEqual(wait_result, True)

    def test_wait_for_stream_token_with_future_sync_token(self) -> None:
        """
        Test `wait_for_stream_token` when we receive a token that is ahead of our
        current token, we'll wait until the stream position advances.

        This can happen if replication streams start lagging, and the client's
        previous sync request was serviced by a worker ahead of ours.
        """
        # We simulate a lagging stream by getting a stream ID from the ID gen
        # and then waiting to mark it as "persisted".
        receipt_id_gen = self.store.get_receipts_stream_id_gen()
        ctx_mgr = receipt_id_gen.get_next()
        receipt_stream_id = self.get_success(ctx_mgr.__aenter__())

        # Create the new token based on the stream ID above.
        current_receipt_token = MultiWriterStreamToken.from_generator(receipt_id_gen)
        receipt_token = current_receipt_token.copy_and_advance(
            MultiWriterStreamToken(stream=receipt_stream_id)
        )
        token = StreamToken.START.copy_and_advance(StreamKeyType.RECEIPT, receipt_token)

        # Function under test
        wait_d = defer.ensureDeferred(self.notifier.wait_for_stream_token(token))

        # This should block waiting for the stream to update
        #
        # Advance time a little bit to make the
        # `wait_for_stream_token(...)` sleep loop iterate.
        self.reactor.advance(Duration(seconds=2).as_secs())
        # It should still not be done yet
        self.assertFalse(wait_d.called)

        # Marking the stream ID as persisted should unblock the request.
        self.get_success(ctx_mgr.__aexit__(None, None, None))

        # Advance time to make another iteration of
        # `wait_for_stream_token(...)` sleep loop so it sees that we're
        # finally caught up now.
        self.reactor.advance(Duration(seconds=1).as_secs())

        # Done waiting and caught-up (True)
        wait_result = self.get_success(wait_d)
        self.assertEqual(wait_result, True)

    def test_wait_for_stream_token_with_future_sync_token_timeout(
        self,
    ) -> None:
        """
        Test `wait_for_stream_token` when we receive a token that is ahead of our
        current token, we'll wait until the stream position advances *until* we hit the
        timeout.

        This can happen if replication streams start lagging, and the client's
        previous sync request was serviced by a worker ahead of ours.
        """
        # We simulate a lagging stream by getting a stream ID from the ID gen
        # and then waiting to mark it as "persisted".
        receipt_id_gen = self.store.get_receipts_stream_id_gen()
        ctx_mgr = receipt_id_gen.get_next()
        receipt_stream_id = self.get_success(ctx_mgr.__aenter__())

        # Create the new token based on the stream ID above.
        current_receipt_token = MultiWriterStreamToken.from_generator(receipt_id_gen)
        receipt_token = current_receipt_token.copy_and_advance(
            MultiWriterStreamToken(stream=receipt_stream_id)
        )
        token = StreamToken.START.copy_and_advance(StreamKeyType.RECEIPT, receipt_token)

        # Function under test
        wait_d = defer.ensureDeferred(self.notifier.wait_for_stream_token(token))
        # Advance time a little bit to make the
        # `wait_for_stream_token(...)` sleep loop record 0 as the `start` time.
        self.reactor.advance(Duration(seconds=0).as_secs())

        # This should block waiting for the stream to update
        #
        # Advance time a little bit to make the
        # `wait_for_stream_token(...)` sleep loop iterate.
        self.reactor.advance(Duration(seconds=5).as_secs())
        # It should still not be done yet (not enough time to hit the timeout)
        self.assertFalse(wait_d.called)
        # Advance time past the 10 second timeout (5 + 6 = 11 seconds) to make the
        # `wait_for_stream_token(...)` sleep loop give up.
        self.reactor.advance(Duration(seconds=6).as_secs())

        # Make sure we gave up waiting and not caught-up (False)
        wait_result = self.get_success(wait_d)
        self.assertEqual(wait_result, False)

    def test_wait_for_stream_token_timeout_counts_each_lagging_stream(self) -> None:
        """
        Test that a timeout while lagging on more than one stream is counted against
        each of them.
        """

        lagging_stream_keys = [StreamKeyType.RECEIPT, StreamKeyType.DEVICE_LIST]

        # Create a new token with the stream IDs artificially advanced far into
        # the future.
        token = StreamToken.START
        token = token.copy_and_advance(
            StreamKeyType.RECEIPT,
            MultiWriterStreamToken(stream=10000000000),  # arbitrarily large
        )
        token = token.copy_and_advance(
            StreamKeyType.DEVICE_LIST,
            MultiWriterStreamToken(stream=10000000000),  # arbitrarily large
        )

        counts_before = self._get_timeout_counts_from_metric()

        wait_d = defer.ensureDeferred(self.notifier.wait_for_stream_token(token))

        # Advance time to make the `wait_for_stream_token(...)` timeout.
        self.reactor.advance(Duration(seconds=11).as_secs())

        # Make sure we gave up waiting and not caught-up (False)
        self.assertEqual(self.get_success(wait_d), False)

        self.assertEqual(
            self._get_timeout_counts_from_metric() - counts_before,
            Counter({stream_key.value: 1 for stream_key in lagging_stream_keys}),
        )

    def _get_timeout_counts_from_metric(self) -> "Counter[str]":
        """The `wait_for_stream_token` timeout counts for this server, keyed by the
        `stream_key` label.

        Useful because the Prometheus metrics are process-wide, and so shared
        between tests. We use this to compare against a count taken before the
        code under test ran.
        """

        # Fetch all samples from the `wait_for_stream_token_timeout_counter`
        # metric. The only label (other than the automatic `server_name` label)
        # is `stream_key`.
        samples = self.get_prometheus_metric_current_values(
            wait_for_stream_token_timeout_counter,
        )

        counts: Counter[str] = Counter()
        for sample in samples:
            counts[sample.labels["stream_key"]] = int(sample.value)

        return counts


class NotifierMemberKeyRoomTestCase(tests.unittest.HomeserverTestCase):
    """MSC4430: a member joining is registered against the room by their user ID.

    The membership event's state key is the member's key, which names no event
    stream: registering that instead leaves a user who was already syncing when
    they joined never woken for the room again, so their /sync long-polls out and
    messages appear only after the timeout.
    """

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4430_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.notifier = hs.get_notifier()
        self.alice = self.register_user("alice", "pass")
        self.alice_token = self.login("alice", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_token = self.login("bob", "pass")

    def _assert_registered_on_join(self, room_version: str | None) -> None:
        room_id = self.helper.create_room_as(
            self.alice, tok=self.alice_token, is_public=True, room_version=room_version
        )

        # Give Bob a live user stream *before* he joins: this is the case the bug
        # hits, as a stream created afterwards is built from `get_rooms_for_user`,
        # which does know about member key rooms.
        self.get_success(
            self.notifier.wait_for_events(
                self.bob, 0, lambda before, after: defer.succeed(True)
            )
        )
        self.assertIn(self.bob, self.notifier.user_to_user_stream)

        self.helper.join(room_id, self.bob, tok=self.bob_token)

        streams = self.notifier.room_to_user_streams.get(room_id, set())
        self.assertIn(
            self.notifier.user_to_user_stream[self.bob],
            streams,
            f"Bob's event stream was not registered against the {room_version} room, "
            "so he will never be woken for events in it",
        )

    def test_join_registers_stream_in_member_key_room(self) -> None:
        self._assert_registered_on_join("org.matrix.msc4430.12")

    def test_join_registers_stream_in_ordinary_room(self) -> None:
        self._assert_registered_on_join(None)

    def _assert_deregistered_on_leave(self, room_version: str | None) -> None:
        room_id = self.helper.create_room_as(
            self.alice, tok=self.alice_token, is_public=True, room_version=room_version
        )
        self.get_success(
            self.notifier.wait_for_events(
                self.bob, 0, lambda before, after: defer.succeed(True)
            )
        )
        bob_stream = self.notifier.user_to_user_stream[self.bob]
        self.helper.join(room_id, self.bob, tok=self.bob_token)
        self.assertIn(room_id, bob_stream.rooms)

        token_before_leave = bob_stream.current_token

        self.helper.leave(room_id, self.bob, tok=self.bob_token)

        # Bob must still have been notified of the leave itself, which is why
        # deregistration happens after the notification rather than during it.
        self.assertNotEqual(
            bob_stream.current_token,
            token_before_leave,
            "Bob's stream was not notified of his own leave",
        )
        self.assertNotIn(
            room_id,
            bob_stream.rooms,
            "Bob's stream is still registered against a room he has left, so he "
            "will be woken for every event in it",
        )
        self.assertNotIn(
            bob_stream, self.notifier.room_to_user_streams.get(room_id, set())
        )

    def test_kick_by_member_key_notifies_and_deregisters(self) -> None:
        """A kick naming the member by key still wakes them, and drops the room.

        Clients may name a member by their key rather than their user ID, which is a
        different path through `parse_membership_target`; check the departing user is
        both notified and deregistered either way.
        """
        room_id = self.helper.create_room_as(
            self.alice,
            tok=self.alice_token,
            is_public=True,
            room_version="org.matrix.msc4430.12",
        )
        self.helper.join(room_id, self.bob, tok=self.bob_token)
        self.get_success(
            self.notifier.wait_for_events(
                self.bob, 0, lambda before, after: defer.succeed(True)
            )
        )
        bob_stream = self.notifier.user_to_user_stream[self.bob]
        self.assertIn(room_id, bob_stream.rooms)

        key = self.get_success(
            self.hs.get_datastores().main.get_local_member_key(room_id, self.bob)
        )
        assert key is not None
        token_before = bob_stream.current_token

        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/rooms/{room_id}/kick",
            {"user_id": key.member_key},
            access_token=self.alice_token,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        self.assertNotEqual(
            bob_stream.current_token,
            token_before,
            "Bob's stream was not notified of a kick naming him by member key",
        )
        self.assertNotIn(room_id, bob_stream.rooms)

    def test_leave_deregisters_stream_in_member_key_room(self) -> None:
        self._assert_deregistered_on_leave("org.matrix.msc4430.12")

    def test_leave_deregisters_stream_in_ordinary_room(self) -> None:
        self._assert_deregistered_on_leave(None)
