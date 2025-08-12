#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
import os

from twisted.test.proto_helpers import MemoryReactor

from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest
from tests.unittest import override_config

try:
    import lxml
except ImportError:
    lxml = None  # type: ignore[assignment]


class URLPreviewTests(unittest.HomeserverTestCase):
    if not lxml:
        skip = "url preview feature requires lxml"

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["url_preview_enabled"] = True
        config["max_spider_size"] = 9999999
        config["url_preview_ip_range_blacklist"] = (
            "192.168.1.1",
            "1.0.0.0/8",
            "3fff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "2001:800::/21",
        )

        self.storage_path = self.mktemp()
        self.media_store_path = self.mktemp()
        os.mkdir(self.storage_path)
        os.mkdir(self.media_store_path)
        config["media_store_path"] = self.media_store_path

        provider_config = {
            "module": "synapse.media.storage_provider.FileStorageProviderBackend",
            "store_local": True,
            "store_synchronous": False,
            "store_remote": True,
            "config": {"directory": self.storage_path},
        }

        config["media_storage_providers"] = [provider_config]

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        media_repo = hs.get_media_repository()
        assert media_repo.url_previewer is not None
        self.url_previewer = media_repo.url_previewer

    def test_all_urls_allowed(self) -> None:
        self.assertFalse(self.url_previewer._is_url_blocked("http://matrix.org"))
        self.assertFalse(self.url_previewer._is_url_blocked("https://matrix.org"))
        self.assertFalse(self.url_previewer._is_url_blocked("http://localhost:8000"))
        self.assertFalse(
            self.url_previewer._is_url_blocked("http://user:pass@matrix.org")
        )

    @override_config(
        {
            "url_preview_url_blacklist": [
                {"username": "user"},
                {"scheme": "http", "netloc": "matrix.org"},
            ]
        }
    )
    def test_blocked_url(self) -> None:
        # Blocked via scheme and URL.
        self.assertTrue(self.url_previewer._is_url_blocked("http://matrix.org"))
        # Not blocked because all components must match.
        self.assertFalse(self.url_previewer._is_url_blocked("https://matrix.org"))

        # Blocked due to the user.
        self.assertTrue(
            self.url_previewer._is_url_blocked("http://user:pass@example.com")
        )
        self.assertTrue(self.url_previewer._is_url_blocked("http://user@example.com"))

    @override_config({"url_preview_url_blacklist": [{"netloc": "*.example.com"}]})
    def test_glob_blocked_url(self) -> None:
        # All subdomains are blocked.
        self.assertTrue(self.url_previewer._is_url_blocked("http://foo.example.com"))
        self.assertTrue(self.url_previewer._is_url_blocked("http://.example.com"))

        # The TLD is not blocked.
        self.assertFalse(self.url_previewer._is_url_blocked("https://example.com"))

    @override_config({"url_preview_url_blacklist": [{"netloc": "^.+\\.example\\.com"}]})
    def test_regex_blocked_urL(self) -> None:
        # All subdomains are blocked.
        self.assertTrue(self.url_previewer._is_url_blocked("http://foo.example.com"))
        # Requires a non-empty subdomain.
        self.assertFalse(self.url_previewer._is_url_blocked("http://.example.com"))

        # The TLD is not blocked.
        self.assertFalse(self.url_previewer._is_url_blocked("https://example.com"))
