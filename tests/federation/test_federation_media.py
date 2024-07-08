#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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
import io
import os
import shutil
import tempfile

from twisted.test.proto_helpers import MemoryReactor

from synapse.media.filepath import MediaFilePaths
from synapse.media.media_storage import MediaStorage
from synapse.media.storage_provider import (
    FileStorageProviderBackend,
    StorageProviderWrapper,
)
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest
from tests.media.test_media_storage import small_png
from tests.test_utils import SMALL_PNG


class FederationMediaDownloadsTest(unittest.FederatingHomeserverTestCase):

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.test_dir = tempfile.mkdtemp(prefix="synapse-tests-")
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.primary_base_path = os.path.join(self.test_dir, "primary")
        self.secondary_base_path = os.path.join(self.test_dir, "secondary")

        hs.config.media.media_store_path = self.primary_base_path

        storage_providers = [
            StorageProviderWrapper(
                FileStorageProviderBackend(hs, self.secondary_base_path),
                store_local=True,
                store_remote=False,
                store_synchronous=True,
            )
        ]

        self.filepaths = MediaFilePaths(self.primary_base_path)
        self.media_storage = MediaStorage(
            hs, self.primary_base_path, self.filepaths, storage_providers
        )
        self.media_repo = hs.get_media_repository()

    def test_file_download(self) -> None:
        content = io.BytesIO(b"file_to_stream")
        content_uri = self.get_success(
            self.media_repo.create_content(
                "text/plain",
                "test_upload",
                content,
                46,
                UserID.from_string("@user_id:whatever.org"),
            )
        )
        # test with a text file
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/media/download/{content_uri.media_id}",
        )
        self.pump()
        self.assertEqual(200, channel.code)

        content_type = channel.headers.getRawHeaders("content-type")
        assert content_type is not None
        assert "multipart/mixed" in content_type[0]
        assert "boundary" in content_type[0]

        # extract boundary
        boundary = content_type[0].split("boundary=")[1]
        # split on boundary and check that json field and expected value exist
        stripped = channel.text_body.split("\r\n" + "--" + boundary)
        # TODO: the json object expected will change once MSC3911 is implemented, currently
        # {} is returned for all requests as a placeholder (per MSC3196)
        found_json = any(
            "\r\nContent-Type: application/json\r\n\r\n{}" in field
            for field in stripped
        )
        self.assertTrue(found_json)

        # check that the text file and expected value exist
        found_file = any(
            "\r\nContent-Type: text/plain\r\nContent-Disposition: inline; filename=test_upload\r\n\r\nfile_to_stream"
            in field
            for field in stripped
        )
        self.assertTrue(found_file)

        content = io.BytesIO(SMALL_PNG)
        content_uri = self.get_success(
            self.media_repo.create_content(
                "image/png",
                "test_png_upload",
                content,
                67,
                UserID.from_string("@user_id:whatever.org"),
            )
        )
        # test with an image file
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/media/download/{content_uri.media_id}",
        )
        self.pump()
        self.assertEqual(200, channel.code)

        content_type = channel.headers.getRawHeaders("content-type")
        assert content_type is not None
        assert "multipart/mixed" in content_type[0]
        assert "boundary" in content_type[0]

        # extract boundary
        boundary = content_type[0].split("boundary=")[1]
        # split on boundary and check that json field and expected value exist
        body = channel.result.get("body")
        assert body is not None
        stripped_bytes = body.split(b"\r\n" + b"--" + boundary.encode("utf-8"))
        found_json = any(
            b"\r\nContent-Type: application/json\r\n\r\n{}" in field
            for field in stripped_bytes
        )
        self.assertTrue(found_json)

        # check that the png file exists and matches what was uploaded
        found_file = any(SMALL_PNG in field for field in stripped_bytes)
        self.assertTrue(found_file)


class FederationThumbnailTest(unittest.FederatingHomeserverTestCase):

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.test_dir = tempfile.mkdtemp(prefix="synapse-tests-")
        self.addCleanup(shutil.rmtree, self.test_dir)
        self.primary_base_path = os.path.join(self.test_dir, "primary")
        self.secondary_base_path = os.path.join(self.test_dir, "secondary")

        hs.config.media.media_store_path = self.primary_base_path

        storage_providers = [
            StorageProviderWrapper(
                FileStorageProviderBackend(hs, self.secondary_base_path),
                store_local=True,
                store_remote=False,
                store_synchronous=True,
            )
        ]

        self.filepaths = MediaFilePaths(self.primary_base_path)
        self.media_storage = MediaStorage(
            hs, self.primary_base_path, self.filepaths, storage_providers
        )
        self.media_repo = hs.get_media_repository()

    def test_thumbnail_download_scaled(self) -> None:
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_content(
                "image/png",
                "test_png_thumbnail",
                content,
                67,
                UserID.from_string("@user_id:whatever.org"),
            )
        )
        # test with an image file
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/media/thumbnail/{content_uri.media_id}?width=32&height=32&method=scale",
        )
        self.pump()
        self.assertEqual(200, channel.code)

        content_type = channel.headers.getRawHeaders("content-type")
        assert content_type is not None
        assert "multipart/mixed" in content_type[0]
        assert "boundary" in content_type[0]

        # extract boundary
        boundary = content_type[0].split("boundary=")[1]
        # split on boundary and check that json field and expected value exist
        body = channel.result.get("body")
        assert body is not None
        stripped_bytes = body.split(b"\r\n" + b"--" + boundary.encode("utf-8"))
        found_json = any(
            b"\r\nContent-Type: application/json\r\n\r\n{}" in field
            for field in stripped_bytes
        )
        self.assertTrue(found_json)

        # check that the png file exists and matches the expected scaled bytes
        found_file = any(small_png.expected_scaled in field for field in stripped_bytes)
        self.assertTrue(found_file)

    def test_thumbnail_download_cropped(self) -> None:
        content = io.BytesIO(small_png.data)
        content_uri = self.get_success(
            self.media_repo.create_content(
                "image/png",
                "test_png_thumbnail",
                content,
                67,
                UserID.from_string("@user_id:whatever.org"),
            )
        )
        # test with an image file
        channel = self.make_signed_federation_request(
            "GET",
            f"/_matrix/federation/v1/media/thumbnail/{content_uri.media_id}?width=32&height=32&method=crop",
        )
        self.pump()
        self.assertEqual(200, channel.code)

        content_type = channel.headers.getRawHeaders("content-type")
        assert content_type is not None
        assert "multipart/mixed" in content_type[0]
        assert "boundary" in content_type[0]

        # extract boundary
        boundary = content_type[0].split("boundary=")[1]
        # split on boundary and check that json field and expected value exist
        body = channel.result.get("body")
        assert body is not None
        stripped_bytes = body.split(b"\r\n" + b"--" + boundary.encode("utf-8"))
        found_json = any(
            b"\r\nContent-Type: application/json\r\n\r\n{}" in field
            for field in stripped_bytes
        )
        self.assertTrue(found_json)

        # check that the png file exists and matches the expected cropped bytes
        found_file = any(
            small_png.expected_cropped in field for field in stripped_bytes
        )
        self.assertTrue(found_file)
