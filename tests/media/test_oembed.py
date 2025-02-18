#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2021 The Matrix.org Foundation C.I.C.
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

import json
from typing import Any

from parameterized import parameterized

from twisted.test.proto_helpers import MemoryReactor

from synapse.media.oembed import OEmbedProvider, OEmbedResult
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.unittest import HomeserverTestCase

try:
    import lxml
except ImportError:
    lxml = None  # type: ignore[assignment]


class OEmbedTests(HomeserverTestCase):
    if not lxml:
        skip = "url preview feature requires lxml"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.oembed = OEmbedProvider(hs)

    def parse_response(self, response: JsonDict) -> OEmbedResult:
        return self.oembed.parse_oembed_response(
            "https://test", json.dumps(response).encode("utf-8")
        )

    def test_version(self) -> None:
        """Accept versions that are similar to 1.0 as a string or int (or missing)."""
        version: Any
        for version in ("1.0", 1.0, 1):
            result = self.parse_response({"version": version})
            # An empty Open Graph response is an error, ensure the URL is included.
            self.assertIn("og:url", result.open_graph_result)

        # A missing version should be treated as 1.0.
        result = self.parse_response({"type": "link"})
        self.assertIn("og:url", result.open_graph_result)

        # Invalid versions should be rejected.
        for version in ("2.0", "1", 1.1, 0, None, {}, []):
            result = self.parse_response({"version": version, "type": "link"})
            # An empty Open Graph response is an error, ensure the URL is included.
            self.assertEqual({}, result.open_graph_result)

    def test_cache_age(self) -> None:
        """Ensure a cache-age is parsed properly."""
        cache_age: Any
        # Correct-ish cache ages are allowed.
        for cache_age in ("1", 1.0, 1):
            result = self.parse_response({"cache_age": cache_age})
            self.assertEqual(result.cache_age, 1000)

        # Invalid cache ages are ignored.
        for cache_age in ("invalid", {}):
            result = self.parse_response({"cache_age": cache_age})
            self.assertIsNone(result.cache_age)

        # Cache age is optional.
        result = self.parse_response({})
        self.assertIsNone(result.cache_age)

    @parameterized.expand(
        [
            ("title", "title"),
            ("provider_name", "site_name"),
            ("thumbnail_url", "image"),
        ],
        name_func=lambda func, num, p: f"{func.__name__}_{p.args[0]}",
    )
    def test_property(self, oembed_property: str, open_graph_property: str) -> None:
        """Test properties which must be strings."""
        result = self.parse_response({oembed_property: "test"})
        self.assertIn(f"og:{open_graph_property}", result.open_graph_result)
        self.assertEqual(result.open_graph_result[f"og:{open_graph_property}"], "test")

        result = self.parse_response({oembed_property: 1})
        self.assertNotIn(f"og:{open_graph_property}", result.open_graph_result)

    def test_author_name(self) -> None:
        """Test the author_name property."""
        result = self.parse_response({"author_name": "test"})
        self.assertEqual(result.author_name, "test")

        result = self.parse_response({"author_name": 1})
        self.assertIsNone(result.author_name)

    def test_rich(self) -> None:
        """Test a type of rich."""
        result = self.parse_response({"html": "test<img src='foo'>", "type": "rich"})
        self.assertIn("og:description", result.open_graph_result)
        self.assertIn("og:image", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:description"], "test")
        self.assertEqual(result.open_graph_result["og:image"], "foo")

        result = self.parse_response({"type": "rich"})
        self.assertNotIn("og:description", result.open_graph_result)

        result = self.parse_response({"html": 1, "type": "rich"})
        self.assertNotIn("og:description", result.open_graph_result)

    def test_photo(self) -> None:
        """Test a type of photo."""
        result = self.parse_response({"url": "test", "type": "photo"})
        self.assertIn("og:image", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:image"], "test")

        result = self.parse_response({"type": "photo"})
        self.assertNotIn("og:image", result.open_graph_result)

        result = self.parse_response({"url": 1, "type": "photo"})
        self.assertNotIn("og:image", result.open_graph_result)

    def test_video(self) -> None:
        """Test a type of video."""
        result = self.parse_response({"html": "test", "type": "video"})
        self.assertIn("og:type", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:type"], "video.other")
        self.assertIn("og:description", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:description"], "test")

        result = self.parse_response({"type": "video"})
        self.assertIn("og:type", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:type"], "video.other")
        self.assertNotIn("og:description", result.open_graph_result)

        result = self.parse_response({"url": 1, "type": "video"})
        self.assertIn("og:type", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:type"], "video.other")
        self.assertNotIn("og:description", result.open_graph_result)

    def test_link(self) -> None:
        """Test type of link."""
        result = self.parse_response({"type": "link"})
        self.assertIn("og:type", result.open_graph_result)
        self.assertEqual(result.open_graph_result["og:type"], "website")

    def test_title_html_entities(self) -> None:
        """Test HTML entities in title"""
        result = self.parse_response(
            {"title": "Why JSON isn&#8217;t a Good Configuration Language"}
        )
        self.assertEqual(
            result.open_graph_result["og:title"],
            "Why JSON isn’t a Good Configuration Language",
        )
