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

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, versions
from synapse.server import HomeServer
from synapse.util.clock import Clock
from synapse.types import JsonDict
from synapse.synapse_rust.http_client import HttpClient

from tests import unittest

logger = logging.getLogger(__name__)


class VersionsTestCase(unittest.HomeserverTestCase):
    """
    Test `VersionsRestServlet`
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        versions.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()

        # XXX: We must create the Rust HTTP client before we call `reactor.run()` below.
        # Twisted's `MemoryReactor` doesn't invoke `callWhenRunning` callbacks if it's
        # already running and we rely on that to start the Tokio thread pool in Rust. In
        # the future, this may not matter, see https://github.com/twisted/twisted/pull/12514
        self._http_client = hs.get_proxied_http_client()
        _ = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )

        # This triggers the server startup hooks, which starts the Tokio thread pool
        reactor.run()

        return hs

    def tearDown(self) -> None:
        # MemoryReactor doesn't trigger the shutdown phases, and we want the
        # Tokio thread pool to be stopped
        # XXX: This logic should probably get moved somewhere else
        shutdown_triggers = self.reactor.triggers.get("shutdown", {})
        for phase in ["before", "during", "after"]:
            triggers = shutdown_triggers.get(phase, [])
            for callbable, args, kwargs in triggers:
                callbable(*args, **kwargs)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.admin_user = self.register_user("admin", "pass", admin=True)
        self.admin_user_tok = self.login("admin", "pass")

    def test_unauthenticated(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
            content={},
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)

    def test_authenticated(self) -> None:
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)

    def test_authenticated_with_per_user_feature(self) -> None:
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        user2_id = self.register_user("user2", "pass")
        user2_tok = self.login(user2_id, "pass")

        # Sanity check that the experimental feature should not be enabled yet
        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)
        self.assertEqual(
            channel.json_body["unstable_features"]["org.matrix.msc3881"],
            False,
            channel.json_body,
        )

        # Enable the feature for this specific user
        self._enable_experimental_feature_for_user(target_user_id=user1_id)

        # The experimental feature should be enabled for this user
        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
            content={},
            access_token=user1_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)
        self.assertEqual(
            channel.json_body["unstable_features"]["org.matrix.msc3881"],
            True,
            channel.json_body,
        )

        # But not for other users
        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
            content={},
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)
        self.assertEqual(
            channel.json_body["unstable_features"]["org.matrix.msc3881"],
            False,
            channel.json_body,
        )

    def _sanity_check_versions_response(self, versions_response: JsonDict) -> None:
        """
        Make sure this looks like a `/_matrix/client/versions` response
        """
        self.assertIsInstance(
            versions_response["versions"],
            list,
            f"Expected `versions` to be a list of strings but saw {versions_response}",
        )
        self.assertIsInstance(
            versions_response["unstable_features"],
            dict,
            f"Expected `unstable_features` to be a dict mapping feature name to a bool but saw {versions_response}",
        )

    def _enable_experimental_feature_for_user(self, *, target_user_id: str) -> None:
        """
        Use the admin API to enable an experimental feature for a specific user
        """
        channel = self.make_request(
            "PUT",
            f"/_synapse/admin/v1/experimental_features/{target_user_id}",
            content={
                "features": {"msc3881": True},
            },
            access_token=self.admin_user_tok,
        )
        self.assertEqual(channel.code, 200)
