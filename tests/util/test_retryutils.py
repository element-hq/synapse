#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from unittest import mock

from synapse.notifier import Notifier
from synapse.replication.tcp.handler import ReplicationCommandHandler
from synapse.util.retryutils import NotRetryingDestination, get_retry_limiter

from tests.unittest import HomeserverTestCase


class RetryLimiterTestCase(HomeserverTestCase):
    def test_new_destination(self) -> None:
        """A happy-path case with a new destination and a successful operation"""
        store = self.hs.get_datastores().main
        limiter = self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            )
        )

        # advance the clock a bit before making the request
        self.pump(1)

        with limiter:
            pass

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        self.assertIsNone(new_timings)

    def test_limiter(self) -> None:
        """General test case which walks through the process of a failing request"""
        store = self.hs.get_datastores().main

        limiter = self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            )
        )

        min_retry_interval_ms = (
            self.hs.config.federation.destination_min_retry_interval_ms
        )
        retry_multiplier = self.hs.config.federation.destination_retry_multiplier

        self.pump(1)
        try:
            with limiter:
                self.pump(1)
                failure_ts = self.clock.time_msec()
                raise AssertionError("argh")
        except AssertionError:
            pass

        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        assert new_timings is not None
        self.assertEqual(new_timings.failure_ts, failure_ts)
        self.assertEqual(new_timings.retry_last_ts, failure_ts)
        self.assertEqual(new_timings.retry_interval, min_retry_interval_ms)

        # now if we try again we should get a failure
        self.get_failure(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            ),
            NotRetryingDestination,
        )

        #
        # advance the clock and try again
        #

        self.pump(min_retry_interval_ms)
        limiter = self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            )
        )

        self.pump(1)
        try:
            with limiter:
                self.pump(1)
                retry_ts = self.clock.time_msec()
                raise AssertionError("argh")
        except AssertionError:
            pass

        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        assert new_timings is not None
        self.assertEqual(new_timings.failure_ts, failure_ts)
        self.assertEqual(new_timings.retry_last_ts, retry_ts)
        self.assertGreaterEqual(
            new_timings.retry_interval, min_retry_interval_ms * retry_multiplier * 0.5
        )
        self.assertLessEqual(
            new_timings.retry_interval, min_retry_interval_ms * retry_multiplier * 2.0
        )

        #
        # one more go, with success
        #
        self.reactor.advance(min_retry_interval_ms * retry_multiplier * 2.0)
        limiter = self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            )
        )

        self.pump(1)
        with limiter:
            self.pump(1)

        # wait for the update to land
        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        self.assertIsNone(new_timings)

    def test_notifier_replication(self) -> None:
        """Ensure the notifier/replication client is called only when expected."""
        store = self.hs.get_datastores().main

        notifier = mock.Mock(spec=Notifier)
        replication_client = mock.Mock(spec=ReplicationCommandHandler)

        limiter = self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
                notifier=notifier,
                replication_client=replication_client,
            )
        )

        # The server is already up, nothing should occur.
        self.pump(1)
        with limiter:
            pass
        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        self.assertIsNone(new_timings)
        notifier.notify_remote_server_up.assert_not_called()
        replication_client.send_remote_server_up.assert_not_called()

        # Attempt again, but return an error. This will cause new retry timings, but
        # should not trigger server up notifications.
        self.pump(1)
        try:
            with limiter:
                raise AssertionError("argh")
        except AssertionError:
            pass
        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        # The exact retry timings are tested separately.
        self.assertIsNotNone(new_timings)
        notifier.notify_remote_server_up.assert_not_called()
        replication_client.send_remote_server_up.assert_not_called()

        # A second failing request should be treated as the above.
        self.pump(1)
        try:
            with limiter:
                raise AssertionError("argh")
        except AssertionError:
            pass
        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        # The exact retry timings are tested separately.
        self.assertIsNotNone(new_timings)
        notifier.notify_remote_server_up.assert_not_called()
        replication_client.send_remote_server_up.assert_not_called()

        # A final successful attempt should generate a server up notification.
        self.pump(1)
        with limiter:
            pass
        self.pump()

        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        # The exact retry timings are tested separately.
        self.assertIsNone(new_timings)
        notifier.notify_remote_server_up.assert_called_once_with("test_dest")
        replication_client.send_remote_server_up.assert_called_once_with("test_dest")

    def test_max_retry_interval(self) -> None:
        """Test that `destination_max_retry_interval` setting works as expected"""
        store = self.hs.get_datastores().main

        destination_max_retry_interval_ms = (
            self.hs.config.federation.destination_max_retry_interval_ms
        )

        self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            )
        )
        self.pump(1)

        failure_ts = self.clock.time_msec()

        # Simulate reaching destination_max_retry_interval
        self.get_success(
            store.set_destination_retry_timings(
                "test_dest",
                failure_ts=failure_ts,
                retry_last_ts=failure_ts,
                retry_interval=destination_max_retry_interval_ms,
            )
        )

        # Check it fails
        self.get_failure(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            ),
            NotRetryingDestination,
        )

        # Get past retry_interval and we can try again, and still throw an error to continue the backoff
        self.reactor.advance(destination_max_retry_interval_ms / 1000 + 1)
        limiter = self.get_success(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            )
        )
        self.pump(1)
        try:
            with limiter:
                self.pump(1)
                raise AssertionError("argh")
        except AssertionError:
            pass

        self.pump()

        # retry_interval does not increase and stays at destination_max_retry_interval_ms
        new_timings = self.get_success(store.get_destination_retry_timings("test_dest"))
        assert new_timings is not None
        self.assertEqual(new_timings.retry_interval, destination_max_retry_interval_ms)

        # Check it fails
        self.get_failure(
            get_retry_limiter(
                destination="test_dest",
                our_server_name=self.hs.hostname,
                clock=self.clock,
                store=store,
            ),
            NotRetryingDestination,
        )
