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

import logging
import urllib.request
from typing import Sequence

from prometheus_client.core import REGISTRY

from synapse.app._base import _handle_metrics_request_error, listen_metrics

from tests import unittest
from tests.metrics._collectors import BrokenCollector, HealthyCollector

logger_name = "synapse.app._base"
metrics_logger_name = "synapse.metrics"


class HandleMetricsRequestErrorTestCase(unittest.TestCase):
    """Tests for `_handle_metrics_request_error`, the `server.handle_error`
    replacement installed by `listen_metrics`."""

    def test_unexpected_exception_is_logged_once_with_traceback(self) -> None:
        """An unexpected error becomes a single ERROR record carrying its traceback."""
        try:
            raise RuntimeError("dictionary changed size during iteration")
        except RuntimeError:
            with self.assertLogs(logger_name, level="ERROR") as cm:
                _handle_metrics_request_error(None, ("127.0.0.1", 12345))

        self.assertEqual(len(cm.records), 1)
        record = cm.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info)
        assert record.exc_info is not None
        self.assertIs(record.exc_info[0], RuntimeError)

    def test_disconnection_errors_are_not_logged_as_errors(self) -> None:
        """A scraper disconnecting is logged at DEBUG, not ERROR."""
        for exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            with self.subTest(exc_type=exc_type):
                try:
                    raise exc_type("boom")
                except exc_type:
                    with self.assertLogs(logger_name, level="DEBUG") as cm:
                        _handle_metrics_request_error(None, ("127.0.0.1", 12345))

                self.assertEqual(len(cm.records), 1)
                self.assertEqual(cm.records[0].levelno, logging.DEBUG)


class ListenMetricsEndToEndTestCase(unittest.TestCase):
    """End-to-end test that a broken collector doesn't stop the rest of the scrape
    against a real `listen_metrics`-started server."""

    def test_scrape_survives_a_broken_collector(self) -> None:
        """A `/metrics` scrape still returns 200 with the healthy metrics when one
        collector raises, logging one ERROR record for it."""
        broken_collector = BrokenCollector("test_metrics_server_broken_collector")
        healthy_collector = HealthyCollector("test_metrics_server_healthy_collector")
        for collector in (broken_collector, healthy_collector):
            REGISTRY.register(collector)
            self.addCleanup(REGISTRY.unregister, collector)

        servers = listen_metrics(["127.0.0.1"], 0)
        for server, thread in servers:
            # Cleanups run last-registered-first, so these run in reverse: shutdown,
            # then server_close, then join.
            self.addCleanup(thread.join, 10)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        host = str(servers[0][0].server_address[0])
        port = servers[0][0].server_address[1]

        with self.assertLogs(metrics_logger_name, level="ERROR") as cm:
            response = urllib.request.urlopen(
                f"http://{host}:{port}/metrics", timeout=10
            )
            self.assertEqual(response.status, 200)
            body = response.read()

        self.assertIn(b"test_metrics_server_healthy_collector", body)
        self.assertNotIn(b"test_metrics_server_broken_collector", body)

        # The scrape runs over the process-global `REGISTRY`, so any other collector
        # failing during it would otherwise be counted.
        records: Sequence[logging.LogRecord] = [
            record
            for record in cm.records
            if "test_metrics_server_broken_collector" in record.getMessage()
        ]
        self.assertEqual(len(records), 1)
