#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from unittest import mock

from twisted.internet.testing import MemoryReactor

from synapse.app.generic_worker import GenericWorkerServer
from synapse.rest.ready import ReadyResource
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest


class ReadyCheckTests(unittest.HomeserverTestCase):
    def create_test_resource(self) -> ReadyResource:
        # replace the JsonResource with a ReadyResource.
        return ReadyResource(self.hs)

    def test_ready_not_started(self) -> None:
        """Before startup has completed, /ready should report unready."""
        channel = self.make_request("GET", "/ready", shorthand=False)

        self.assertEqual(channel.code, 503)
        self.assertEqual(
            channel.json_body,
            {"db": True, "replication": True, "startup_complete": False},
        )

    def test_ready_all_ok(self) -> None:
        self.hs.set_synapse_started()

        channel = self.make_request("GET", "/ready", shorthand=False)

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {"db": True, "replication": True, "startup_complete": True},
        )

    def test_ready_path_traversal(self) -> None:
        channel = self.make_request("GET", "/ready/extra/path", shorthand=False)

        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], "M_UNRECOGNIZED")
        self.assertIn("error", channel.json_body)

    def test_ready_db_down(self) -> None:
        self.hs.set_synapse_started()

        store = self.hs.get_datastores().main
        with mock.patch.object(store.db_pool, "execute", side_effect=Exception("boom")):
            channel = self.make_request("GET", "/ready", shorthand=False)

        self.assertEqual(channel.code, 503)
        self.assertEqual(
            channel.json_body,
            {"db": False, "replication": True, "startup_complete": True},
        )

    def test_ready_main_process_ignores_replication_connected(self) -> None:
        self.hs.set_synapse_started()

        with mock.patch.object(
            self.hs.get_replication_command_handler(),
            "connected",
            return_value=False,
        ):
            channel = self.make_request("GET", "/ready", shorthand=False)

        self.assertEqual(channel.code, 200)
        self.assertTrue(channel.json_body["replication"])


class WorkerReadyCheckTests(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        return self.setup_test_homeserver(homeserver_to_use=GenericWorkerServer)

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        conf["worker_app"] = "synapse.app.generic_worker"
        conf["instance_map"] = {"main": {"host": "127.0.0.1", "port": 0}}
        return conf

    def create_test_resource(self) -> ReadyResource:
        return ReadyResource(self.hs)

    def test_ready_replication_down(self) -> None:
        self.hs.set_synapse_started()

        with mock.patch.object(
            self.hs.get_replication_command_handler(),
            "connected",
            return_value=False,
        ):
            channel = self.make_request("GET", "/ready", shorthand=False)

        self.assertEqual(channel.code, 503)
        self.assertEqual(
            channel.json_body,
            {"db": True, "replication": False, "startup_complete": True},
        )

    def test_ready_replication_up(self) -> None:
        self.hs.set_synapse_started()

        with mock.patch.object(
            self.hs.get_replication_command_handler(),
            "connected",
            return_value=True,
        ):
            channel = self.make_request("GET", "/ready", shorthand=False)

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {"db": True, "replication": True, "startup_complete": True},
        )
