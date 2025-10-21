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
from typing import Dict

from twisted.internet.testing import MemoryReactor
from twisted.web.resource import Resource

from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest


class SignedDownloadTestCase(unittest.HomeserverTestCase):
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

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.media_repo = hs.get_media_repository()

    def test_valid_signed_download(self) -> None:
        """Test that a valid signed URL returns the media content"""
        # Create test content
        content = io.BytesIO(b"test file content")
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "text/plain",
                "some_name.txt",
                content,
                17,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL
        exp = self.clock.time_msec() + 3600000  # 1 hour from now
        key = self.media_repo.download_media_key(
            media_id=content_uri.media_id, exp=exp, name="test_file.txt"
        )
        sig = self.media_repo.compute_media_request_signature(key)

        # Make the request
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}/test_file.txt?exp={exp}&sig={sig}",
            shorthand=False,
        )

        # Check the response
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.result["body"], b"test file content")

        # Check content type
        content_type = channel.headers.getRawHeaders("Content-Type")
        assert content_type is not None
        self.assertIn("text/plain", content_type[0])

        # Check content disposition
        content_disposition = channel.headers.getRawHeaders("Content-Disposition")
        assert content_disposition is not None
        self.assertIn("test_file.txt", content_disposition[0])

    def test_valid_signed_download_without_filename(self) -> None:
        """Test that a valid signed URL works without a filename"""
        # Create test content
        content = io.BytesIO(b"test file content")
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "text/plain",
                "test_file.txt",
                content,
                17,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL without filename
        exp = self.clock.time_msec() + 3600000  # 1 hour from now
        key = self.media_repo.download_media_key(
            media_id=content_uri.media_id, exp=exp, name=None
        )
        sig = self.media_repo.compute_media_request_signature(key)

        # Make the request
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}?exp={exp}&sig={sig}",
            shorthand=False,
        )

        # Check the response
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.result["body"], b"test file content")

    def test_invalid_signature(self) -> None:
        """Test that an invalid signature returns 404"""
        # Create test content
        content = io.BytesIO(b"test file content")
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "text/plain",
                "test_file.txt",
                content,
                17,
                UserID.from_string("@user:test"),
            )
        )

        # Use a properly formatted but invalid signature (64 hex chars like a real signature)
        exp = self.clock.time_msec() + 3600000  # 1 hour from now
        invalid_sig = "0" * 64  # Invalid but properly formatted signature

        # Make the request
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}?exp={exp}&sig={invalid_sig}",
            shorthand=False,
        )

        # Check the response
        self.assertEqual(channel.code, 404)

    def test_expired_url(self) -> None:
        """Test that an expired URL returns 404"""
        # Create test content
        content = io.BytesIO(b"test file content")
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "text/plain",
                "test_file.txt",
                content,
                17,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL that will expire soon
        exp = self.clock.time_msec() + 1000  # 1 second from now
        key = self.media_repo.download_media_key(
            media_id=content_uri.media_id, exp=exp, name=None
        )
        sig = self.media_repo.compute_media_request_signature(key)

        # Advance the clock to make the URL expired
        self.reactor.advance(2)  # Advance 2 seconds

        # Make the request
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}?exp={exp}&sig={sig}",
            shorthand=False,
        )

        # Check the response
        self.assertEqual(channel.code, 404)

    def test_missing_parameters(self) -> None:
        """Test that missing exp or sig parameters return 404"""
        # Create test content
        content = io.BytesIO(b"test file content")
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "text/plain",
                "test_file.txt",
                content,
                17,
                UserID.from_string("@user:test"),
            )
        )

        # Test missing exp parameter
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}?sig=somesig",
            shorthand=False,
        )
        self.assertEqual(channel.code, 400)  # Bad request for missing required param

        # Test missing sig parameter
        exp = self.clock.time_msec() + 3600000
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}?exp={exp}",
            shorthand=False,
        )
        self.assertEqual(channel.code, 400)  # Bad request for missing required param

    def test_nonexistent_media(self) -> None:
        """Test that requesting non-existent media returns 404"""
        # Generate a signed URL for non-existent media
        fake_media_id = "nonexistent"
        exp = self.clock.time_msec() + 3600000  # 1 hour from now
        key = self.media_repo.download_media_key(
            media_id=fake_media_id, exp=exp, name=None
        )
        sig = self.media_repo.compute_media_request_signature(key)

        # Make the request
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{fake_media_id}?exp={exp}&sig={sig}",
            shorthand=False,
        )

        # Check the response
        self.assertEqual(channel.code, 404)

    def test_etag_functionality(self) -> None:
        """Test that ETag functionality works properly"""
        # Create test content
        content = io.BytesIO(b"test file content for etag")
        content_uri = self.get_success(
            self.media_repo.create_or_update_content(
                "text/plain",
                "test_file.txt",
                content,
                26,
                UserID.from_string("@user:test"),
            )
        )

        # Generate a signed URL
        exp = self.clock.time_msec() + 3600000  # 1 hour from now
        key = self.media_repo.download_media_key(
            media_id=content_uri.media_id, exp=exp, name=None
        )
        sig = self.media_repo.compute_media_request_signature(key)

        # Make the first request
        channel = self.make_request(
            "GET",
            f"/_synapse/media/download/{content_uri.media_id}?exp={exp}&sig={sig}",
            shorthand=False,
        )

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
            f"/_synapse/media/download/{content_uri.media_id}?exp={exp}&sig={sig}",
            shorthand=False,
            custom_headers=[("If-None-Match", etag)],
        )

        # Should get 304 Not Modified
        self.assertEqual(channel.code, 304)
        self.assertNotIn("body", channel.result)
