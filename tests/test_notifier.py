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

from twisted.internet import defer
from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.types import MultiWriterStreamToken, StreamKeyType, StreamToken
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
