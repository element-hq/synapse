#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
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

import io
import re
from typing import Dict
from urllib.parse import urlencode

from twisted.internet.testing import MemoryReactor
from twisted.web.resource import Resource

from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest
from tests.media.test_media_storage import small_png


class SignedThumbnailTestCase(unittest.HomeserverTestCase):
    def create_resource_dict(self) -> Dict[str, Resource]:
        d = super().create_resource_dict()
        d.update(build_synapse_client_resource_tree(self.hs))
        return d

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["media_redirect"] = {
            "enabled": True,
            "secret": "supersecret",
        }
        config["dynamic_thumbnails"] = True

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.media_repo = hs.get_media_repository()

    def test_valid_signed_thumbnail_scaled(self) -> None:
        """Test that a valid signed URL returns the thumbnail content (scaled)"""
        # Create test content with an image
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "image/png",
                "test_image.png",
                content,
                67,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL for scaled thumbnail
        params_dict = {
            "width": "32",
            "height": "32",
            "method": "scale",
            "type": "image/png",
        }
        signed_url = self.media_repo.signed_location_for_thumbnail(
            media_id=content_uri.media_id, parameters=params_dict
        )
        # Extract the path and query from the signed URL
        url_path = signed_url.split("https://test", 1)[1]

        # Make the request
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response
        self.assertEqual(channel.code, 200)

        # Check content type
        content_type = channel.headers.getRawHeaders("Content-Type")
        assert content_type is not None
        self.assertEqual(content_type[0], "image/png")

        # Check that we got actual thumbnail data
        self.assertIsNotNone(channel.result.get("body"))
        self.assertGreater(len(channel.result["body"]), 0)

    def test_valid_signed_thumbnail_cropped(self) -> None:
        """Test that a valid signed URL returns the thumbnail content (cropped)"""
        # Create test content with an image
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "image/png",
                "test_image.png",
                content,
                67,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL for cropped thumbnail
        params_dict = {
            "width": "32",
            "height": "32",
            "method": "crop",
            "type": "image/png",
        }
        signed_url = self.media_repo.signed_location_for_thumbnail(
            media_id=content_uri.media_id, parameters=params_dict
        )
        # Extract the path and query from the signed URL
        url_path = signed_url.split("https://test", 1)[1]

        # Make the request
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response
        self.assertEqual(channel.code, 200)

        # Check content type
        content_type = channel.headers.getRawHeaders("Content-Type")
        assert content_type is not None
        self.assertEqual(content_type[0], "image/png")

        # Check that we got actual thumbnail data
        self.assertIsNotNone(channel.result.get("body"))
        self.assertGreater(len(channel.result["body"]), 0)

    def test_invalid_signature(self) -> None:
        """Test that an invalid signature returns 404"""
        # Create test content with an image
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "image/png",
                "test_image.png",
                content,
                67,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL
        params_dict = {
            "width": "32",
            "height": "32",
        }
        signed_url = self.media_repo.signed_location_for_thumbnail(
            media_id=content_uri.media_id, parameters=params_dict
        )

        # Extract the path and query from the signed URL
        url_path = signed_url.split("https://test", 1)[1]
        invalid_sig = "0" * 64  # Invalid but properly formatted signature
        url_path = re.sub(r"sig=\w+", "sig=" + invalid_sig, url_path)

        # Make the request
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response
        self.assertEqual(channel.code, 404)

    def test_expired_url(self) -> None:
        """Test that an expired URL returns 404"""
        # Create test content with an image
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "image/png",
                "test_image.png",
                content,
                67,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL
        params_dict = {
            "width": "32",
            "height": "32",
        }
        signed_url = self.media_repo.signed_location_for_thumbnail(
            media_id=content_uri.media_id, parameters=params_dict
        )
        # Extract the path and query from the signed URL
        url_path = signed_url.split("https://test", 1)[1]

        # Make a first request, it should work
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response
        self.assertEqual(channel.code, 200)

        # Advance the clock to make the URL expired
        self.reactor.advance(
            10 * 60 + 1
        )  # Advance 10 minutes + 1 second (TTL is 10 minutes by default)

        # Make a second request, it should fail
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response
        self.assertEqual(channel.code, 404)

    def test_missing_parameters(self) -> None:
        """Test that missing exp or sig parameters return 400"""
        # Create test content with an image
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "image/png",
                "test_image.png",
                content,
                67,
                UserID.from_string("@user:test"),
            )
        )

        params_dict = {
            "width": "32",
            "height": "32",
            "method": "scale",
            "type": "image/png",
        }
        parameters = urlencode(params_dict)

        # Test missing exp parameter
        channel = self.make_request(
            "GET",
            f"/_synapse/media/thumbnail/{content_uri.media_id}/{parameters}?sig=somesig",
            shorthand=False,
        )
        self.pump()
        self.assertEqual(channel.code, 400)  # Bad request for missing required param

        # Test missing sig parameter
        exp = self.clock.time_msec() + 3600000
        channel = self.make_request(
            "GET",
            f"/_synapse/media/thumbnail/{content_uri.media_id}/{parameters}?exp={exp}",
            shorthand=False,
        )
        self.pump()
        self.assertEqual(channel.code, 400)  # Bad request for missing required param

    def test_nonexistent_media(self) -> None:
        """Test that requesting non-existent media returns 404"""
        # Generate a signed URL for non-existent media
        fake_media_id = "nonexistent"
        params_dict = {
            "width": "32",
            "height": "32",
        }
        signed_url = self.media_repo.signed_location_for_thumbnail(
            media_id=fake_media_id, parameters=params_dict
        )
        # Extract the path and query from the signed URL
        url_path = signed_url.split("https://test", 1)[1]

        # Make the request
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response
        self.assertEqual(channel.code, 404)

    def test_etag_functionality(self) -> None:
        """Test that ETag functionality works properly"""
        # Create test content with an image
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "image/png",
                "test_image.png",
                content,
                67,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL
        params_dict = {
            "width": "32",
            "height": "32",
        }
        signed_url = self.media_repo.signed_location_for_thumbnail(
            media_id=content_uri.media_id, parameters=params_dict
        )
        # Extract the path and query from the signed URL
        url_path = signed_url.split("https://test", 1)[1]

        # Make a first request
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
        )
        self.pump()

        # Check the response has an ETag and a Cache-Control header
        self.assertEqual(channel.code, 200)
        etag_headers = channel.headers.getRawHeaders("ETag")
        assert etag_headers is not None
        etag = etag_headers[0]
        cache_control = channel.headers.getRawHeaders("Cache-Control")
        self.assertIsNotNone(cache_control)

        # Make a second request with If-None-Match header
        channel = self.make_request(
            "GET",
            url_path,
            shorthand=False,
            custom_headers=[("If-None-Match", etag)],
        )
        self.pump()

        # Should get 304 Not Modified
        self.assertEqual(channel.code, 304)
        self.assertNotIn("body", channel.result)
