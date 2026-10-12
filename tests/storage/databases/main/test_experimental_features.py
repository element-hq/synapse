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
#

from twisted.internet.testing import MemoryReactor

from synapse.rest.admin.experimental_features import ExperimentalFeature
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest


class ExperimentalFeaturesStoreTestCase(unittest.HomeserverTestCase):
    """`is_feature_enabled` reads through a cache that lives in Rust."""

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.user_id = self.get_success(
            hs.get_registration_handler().register_user(localpart="user")
        )

    def _is_enabled(self, feature: ExperimentalFeature) -> bool:
        return self.get_success(self.store.is_feature_enabled(self.user_id, feature))

    def test_reads_through_the_rust_cache(self) -> None:
        cache = self.store.is_feature_enabled_for_user

        # No row means disabled, and that answer is cached too.
        self.assertFalse(self._is_enabled(ExperimentalFeature.MSC3881))
        self.assertEqual((cache.hits, cache.misses), (0, 1))
        self.assertFalse(self._is_enabled(ExperimentalFeature.MSC3881))
        self.assertEqual((cache.hits, cache.misses), (1, 1))

        # Writing invalidates, so the next read sees the new row...
        self.get_success(
            self.store.set_features_for_user(
                self.user_id, {ExperimentalFeature.MSC3881: True}
            )
        )
        self.assertTrue(self._is_enabled(ExperimentalFeature.MSC3881))
        self.assertEqual((cache.hits, cache.misses), (1, 2))

        # ...in both directions.
        self.get_success(
            self.store.set_features_for_user(
                self.user_id, {ExperimentalFeature.MSC3881: False}
            )
        )
        self.assertFalse(self._is_enabled(ExperimentalFeature.MSC3881))

    def test_invalidation_with_plain_string_keys(self) -> None:
        """Invalidations from other workers arrive as strings.

        The cache invalidation stream's column is `keys text[]`, so whatever a
        key started life as, a worker receiving it sees a string, and the Rust
        side has to decode that back into its own key type.
        """
        cache = self.store.is_feature_enabled_for_user
        self._is_enabled(ExperimentalFeature.MSC3881)
        self.assertEqual(len(cache), 1)

        # A key that matches nothing is a no-op rather than an error: a worker
        # may well see feature names this version doesn't know about.
        cache.invalidate((self.user_id, "not_a_feature"))
        cache.invalidate(("@nobody:test", "msc3881"))
        self.assertEqual(len(cache), 1)

        # So is a key of the wrong shape, which a newer worker might stream. A
        # Python cache shrugs those off, and raising here would fail the whole
        # batch of invalidations the row arrived in.
        cache.invalidate((self.user_id,))
        cache.invalidate((self.user_id, "msc3881", "extra"))
        cache.invalidate((self.user_id, 42))
        self.assertEqual(len(cache), 1)

        cache.invalidate((self.user_id, "msc3881"))
        self.assertEqual(len(cache), 0)

    def test_an_unknown_feature_is_an_error(self) -> None:
        """The Rust side keys the cache by its own enum of features, which
        mirrors `ExperimentalFeature`. A name it doesn't know means the two
        have drifted apart, and should fail rather than quietly read as off."""
        with self.assertRaises(ValueError):
            self.store.is_feature_enabled_for_user(self.user_id, "not_a_feature")

    @unittest.override_config({"experimental_features": {"msc3881_enabled": True}})
    def test_a_globally_enabled_feature_skips_the_lookup(self) -> None:
        cache = self.store.is_feature_enabled_for_user

        self.assertTrue(self._is_enabled(ExperimentalFeature.MSC3881))
        self.assertEqual((cache.hits, cache.misses), (0, 0))
