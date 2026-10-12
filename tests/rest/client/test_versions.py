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
from synapse.types import JsonDict
from synapse.util.caches.lrucache import setup_expire_lru_cache_entries
from synapse.util.clock import Clock

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
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)

    def test_authenticated(self) -> None:
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
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
        self._enable_experimental_feature_for_user(
            target_user_id=user1_id, features={"msc3881": True}
        )

        # The experimental feature should be enabled for this user
        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
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
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self._sanity_check_versions_response(channel.json_body)
        self.assertEqual(
            channel.json_body["unstable_features"]["org.matrix.msc3881"],
            False,
            channel.json_body,
        )

    def test_msc4446_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(channel.json_body["unstable_features"]["com.beeper.msc4446"])

    @unittest.override_config({"experimental_features": {"msc4446_enabled": True}})
    def test_msc4446_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(channel.json_body["unstable_features"]["com.beeper.msc4446"])

    def test_msc4502_false_by_default(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertFalse(channel.json_body["unstable_features"]["io.element.msc4502"])

    @unittest.override_config({"experimental_features": {"msc4502_enabled": True}})
    def test_msc4502_true_if_enabled(self) -> None:
        channel = self.make_request("GET", "/_matrix/client/versions")
        self.assertEqual(channel.code, 200, channel.result)
        self.assertTrue(channel.json_body["unstable_features"]["io.element.msc4502"])

    def test_per_user_feature_can_be_disabled_again(self) -> None:
        """The Rust cache must be invalidated when a feature is turned back off.

        Enabling is the easier direction, because the value being replaced is
        the "no row for this user" one. Turning a feature back off only works
        if invalidation reaches an entry that was populated from a real row.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        self._enable_experimental_feature_for_user(
            target_user_id=user1_id, features={"msc3881": True}
        )
        self.assertTrue(self._unstable_feature(user1_tok, "org.matrix.msc3881"))

        self._enable_experimental_feature_for_user(
            target_user_id=user1_id, features={"msc3881": False}
        )
        self.assertFalse(self._unstable_feature(user1_tok, "org.matrix.msc3881"))

    # `msc3575_enabled` defaults to True, in which case the per-user lookup is
    # skipped altogether. Turn it off so that this feature actually reaches
    # the cache.
    @unittest.override_config({"experimental_features": {"msc3575_enabled": False}})
    def test_per_user_msc3575(self) -> None:
        """The other feature `/versions` looks up per user, which is otherwise
        only covered when it is globally enabled."""
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        feature = "org.matrix.simplified_msc3575"
        self.assertFalse(self._unstable_feature(user1_tok, feature))

        self._enable_experimental_feature_for_user(
            target_user_id=user1_id, features={"msc3575": True}
        )
        self.assertTrue(self._unstable_feature(user1_tok, feature))

        self._enable_experimental_feature_for_user(
            target_user_id=user1_id, features={"msc3575": False}
        )
        self.assertFalse(self._unstable_feature(user1_tok, feature))

    @unittest.override_config({"caches": {"expiry_time": "30m"}})
    def test_cache_entries_expire_when_unread(self) -> None:
        """The Rust cache must take part in Synapse's age-based cache expiry,
        off the same looping call and the same config as the Python ones."""
        setup_expire_lru_cache_entries(self.hs)

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        cache = self.hs.get_datastores().main.is_feature_enabled_for_user
        self._unstable_feature(user1_tok, "org.matrix.msc3881")
        self.assertEqual(len(cache), 1)

        # Not stale yet.
        self.reactor.advance(20 * 60)
        self.assertEqual(len(cache), 1)

        # Now it has gone 30 minutes unread.
        self.reactor.advance(20 * 60)
        self.assertEqual(len(cache), 0)

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

    def _enable_experimental_feature_for_user(
        self, *, target_user_id: str, features: dict[str, bool]
    ) -> None:
        """
        Use the admin API to enable an experimental feature for a specific user
        """
        channel = self.make_request(
            "PUT",
            f"/_synapse/admin/v1/experimental_features/{target_user_id}",
            content={
                "features": features,
            },
            access_token=self.admin_user_tok,
        )
        self.assertEqual(channel.code, 200)

    def _unstable_feature(self, access_token: str, feature: str) -> bool:
        """Ask `/versions` whether `feature` is on for whoever holds this token."""
        channel = self.make_request(
            "GET",
            "/_matrix/client/versions",
            access_token=access_token,
        )
        self.assertEqual(channel.code, 200, channel.result)
        return channel.json_body["unstable_features"][feature]
