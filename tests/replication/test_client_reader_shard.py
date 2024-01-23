#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from synapse.rest.client import register

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import make_request

logger = logging.getLogger(__name__)


class ClientReaderTestCase(BaseMultiWorkerStreamTestCase):
    """Test using one or more generic workers for registration."""

    servlets = [register.register_servlets]

    def _get_worker_hs_config(self) -> dict:
        config = self.default_config()
        config["worker_app"] = "synapse.app.generic_worker"
        return config

    def test_register_single_worker(self) -> None:
        """Test that registration works when using a single generic worker."""
        worker_hs = self.make_worker_hs("synapse.app.generic_worker")
        site = self._hs_to_site[worker_hs]

        channel_1 = make_request(
            self.reactor,
            site,
            "POST",
            "register",
            {"username": "user", "type": "m.login.password", "password": "bar"},
        )
        self.assertEqual(channel_1.code, 401)

        # Grab the session
        session = channel_1.json_body["session"]

        # also complete the dummy auth
        channel_2 = make_request(
            self.reactor,
            site,
            "POST",
            "register",
            {"auth": {"session": session, "type": "m.login.dummy"}},
        )
        self.assertEqual(channel_2.code, 200)

        # We're given a registered user.
        self.assertEqual(channel_2.json_body["user_id"], "@user:test")

    def test_register_multi_worker(self) -> None:
        """Test that registration works when using multiple generic workers."""
        worker_hs_1 = self.make_worker_hs("synapse.app.generic_worker")
        worker_hs_2 = self.make_worker_hs("synapse.app.generic_worker")

        site_1 = self._hs_to_site[worker_hs_1]
        channel_1 = make_request(
            self.reactor,
            site_1,
            "POST",
            "register",
            {"username": "user", "type": "m.login.password", "password": "bar"},
        )
        self.assertEqual(channel_1.code, 401)

        # Grab the session
        session = channel_1.json_body["session"]

        # also complete the dummy auth
        site_2 = self._hs_to_site[worker_hs_2]
        channel_2 = make_request(
            self.reactor,
            site_2,
            "POST",
            "register",
            {"auth": {"session": session, "type": "m.login.dummy"}},
        )
        self.assertEqual(channel_2.code, 200)

        # We're given a registered user.
        self.assertEqual(channel_2.json_body["user_id"], "@user:test")
