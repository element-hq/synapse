#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2018-2021 The Matrix.org Foundation C.I.C.
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
import shutil
import tempfile
from binascii import unhexlify
from io import BytesIO
from typing import Any, BinaryIO, ClassVar, Literal
from unittest.mock import MagicMock, Mock, patch
from urllib import parse

import attr
from parameterized import parameterized, parameterized_class
from PIL import Image as Image

from twisted.internet import defer
from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor
from twisted.python.failure import Failure
from twisted.web.http_headers import Headers
from twisted.web.iweb import UNKNOWN_LENGTH, IResponse
from twisted.web.resource import Resource

from synapse.api.errors import Codes, HttpResponseException
from synapse.api.ratelimiting import Ratelimiter
from synapse.events import EventBase
from synapse.http.client import ByteWriteable
from synapse.http.types import QueryParams
from synapse.logging.context import make_deferred_yieldable
from synapse.media._base import FileInfo, ThumbnailInfo
from synapse.media.filepath import MediaFilePaths
from synapse.media.media_storage import MediaStorage, ReadableFileWrapper
from synapse.media.storage_provider import FileStorageProviderBackend
from synapse.media.thumbnailer import ThumbnailProvider
from synapse.module_api import ModuleApi
from synapse.module_api.callbacks.spamchecker_callbacks import load_legacy_spam_checkers
from synapse.rest import admin
from synapse.rest.client import login, media
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomAlias
from synapse.util.clock import Clock

from tests import unittest
from tests.server import FakeChannel
from tests.test_utils import SMALL_CMYK_JPEG, SMALL_PNG, SMALL_PNG_SHA256
from tests.unittest import override_config
from tests.utils import default_config


class MediaStorageTests(unittest.HomeserverTestCase):
    needs_threadpool = True

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="synapse-tests-")
        self.addCleanup(shutil.rmtree, self.test_dir)

        self.primary_base_path = os.path.join(self.test_dir, "primary")
        self.secondary_base_path = os.path.join(self.test_dir, "secondary")

        hs.config.media.media_store_path = self.primary_base_path

        storage_providers = [FileStorageProviderBackend(hs, self.secondary_base_path)]

        self.filepaths = MediaFilePaths(self.primary_base_path)
        self.media_storage = MediaStorage(
            hs, self.primary_base_path, self.filepaths, storage_providers
        )

    def test_ensure_media_is_in_local_cache(self) -> None:
        media_id = "some_media_id"
        test_body = "Test\n"

        # First we create a file that is in a storage provider but not in the
        # local primary media store
        rel_path = self.filepaths.local_media_filepath_rel(media_id)
        secondary_path = os.path.join(self.secondary_base_path, rel_path)

        os.makedirs(os.path.dirname(secondary_path))

        with open(secondary_path, "w") as f:
            f.write(test_body)

        # Now we run ensure_media_is_in_local_cache, which should copy the file
        # to the local cache.
        file_info = FileInfo(None, media_id)

        # This uses a real blocking threadpool so we have to wait for it to be
        # actually done :/
        x = defer.ensureDeferred(
            self.media_storage.ensure_media_is_in_local_cache(file_info)
        )

        # Hotloop until the threadpool does its job...
        self.wait_on_thread(x)

        local_path = self.get_success(x)

        self.assertTrue(os.path.exists(local_path))

        # Asserts the file is under the expected local cache directory
        self.assertEqual(
            os.path.commonprefix([self.primary_base_path, local_path]),
            self.primary_base_path,
        )

        with open(local_path) as f:
            body = f.read()

        self.assertEqual(test_body, body)


@attr.s(auto_attribs=True, slots=True, frozen=True)
class TestImage:
    """An image for testing thumbnailing with the expected results

    Attributes:
        data: The raw image to thumbnail
        content_type: The type of the image as a content type, e.g. "image/png"
        extension: The extension associated with the format, e.g. ".png"
        expected_cropped: The expected bytes from cropped thumbnailing, or None if
            test should just check for success.
        expected_scaled: The expected bytes from scaled thumbnailing, or None if
            test should just check for a valid image returned.
        expected_found: True if the file should exist on the server, or False if
            a 404/400 is expected.
        unable_to_thumbnail: True if we expect the thumbnailing to fail (400), or
            False if the thumbnailing should succeed or a normal 404 is expected.
        is_inline: True if we expect the file to be served using an inline
            Content-Disposition or False if we expect an attachment.
    """

    data: bytes
    content_type: bytes
    extension: bytes
    expected_cropped: bytes | None = None
    expected_scaled: bytes | None = None
    expected_found: bool = True
    unable_to_thumbnail: bool = False
    is_inline: bool = True


small_png = TestImage(
    SMALL_PNG,
    b"image/png",
    b".png",
    unhexlify(
        b"89504e470d0a1a0a0000000d4948445200000020000000200806"
        b"000000737a7af40000001a49444154789cedc101010000008220"
        b"ffaf6e484001000000ef0610200001194334ee0000000049454e"
        b"44ae426082"
    ),
    unhexlify(
        b"89504e470d0a1a0a0000000d4948445200000001000000010806"
        b"0000001f15c4890000000d49444154789c636060606000000005"
        b"0001a5f645400000000049454e44ae426082"
    ),
)

small_png_with_transparency = TestImage(
    unhexlify(
        b"89504e470d0a1a0a0000000d49484452000000010000000101000"
        b"00000376ef9240000000274524e5300010194fdae0000000a4944"
        b"4154789c636800000082008177cd72b60000000049454e44ae426"
        b"082"
    ),
    b"image/png",
    b".png",
    # Note that we don't check the output since it varies across
    # different versions of Pillow.
)

small_cmyk_jpeg = TestImage(
    SMALL_CMYK_JPEG,
    b"image/jpeg",
    b".jpeg",
    # These values were sourced simply by seeing at what the tests produced at
    # the time of writing. If this changes, the tests will fail.
    unhexlify(
        b"ffd8ffe000104a46494600010100000100010000ffdb00430006"
        b"040506050406060506070706080a100a0a09090a140e0f0c1017"
        b"141818171416161a1d251f1a1b231c1616202c20232627292a29"
        b"191f2d302d283025282928ffdb0043010707070a080a130a0a13"
        b"281a161a28282828282828282828282828282828282828282828"
        b"2828282828282828282828282828282828282828282828282828"
        b"2828ffc00011080020002003012200021101031101ffc4001f00"
        b"0001050101010101010000000000000000010203040506070809"
        b"0a0bffc400b5100002010303020403050504040000017d010203"
        b"00041105122131410613516107227114328191a1082342b1c115"
        b"52d1f02433627282090a161718191a25262728292a3435363738"
        b"393a434445464748494a535455565758595a636465666768696a"
        b"737475767778797a838485868788898a92939495969798999aa2"
        b"a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9ca"
        b"d2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7"
        b"f8f9faffc4001f01000301010101010101010100000000000001"
        b"02030405060708090a0bffc400b5110002010204040304070504"
        b"0400010277000102031104052131061241510761711322328108"
        b"144291a1b1c109233352f0156272d10a162434e125f11718191a"
        b"262728292a35363738393a434445464748494a53545556575859"
        b"5a636465666768696a737475767778797a82838485868788898a"
        b"92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9"
        b"bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8"
        b"e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fa"
        b"a68a28a0028a28a0028a28a0028a28a00fffd9"
    ),
    unhexlify(
        b"ffd8ffe000104a46494600010100000100010000ffdb00430006"
        b"040506050406060506070706080a100a0a09090a140e0f0c1017"
        b"141818171416161a1d251f1a1b231c1616202c20232627292a29"
        b"191f2d302d283025282928ffdb0043010707070a080a130a0a13"
        b"281a161a28282828282828282828282828282828282828282828"
        b"2828282828282828282828282828282828282828282828282828"
        b"2828ffc00011080001000103012200021101031101ffc4001f00"
        b"0001050101010101010000000000000000010203040506070809"
        b"0a0bffc400b5100002010303020403050504040000017d010203"
        b"00041105122131410613516107227114328191a1082342b1c115"
        b"52d1f02433627282090a161718191a25262728292a3435363738"
        b"393a434445464748494a535455565758595a636465666768696a"
        b"737475767778797a838485868788898a92939495969798999aa2"
        b"a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9ca"
        b"d2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7"
        b"f8f9faffc4001f01000301010101010101010100000000000001"
        b"02030405060708090a0bffc400b5110002010204040304070504"
        b"0400010277000102031104052131061241510761711322328108"
        b"144291a1b1c109233352f0156272d10a162434e125f11718191a"
        b"262728292a35363738393a434445464748494a53545556575859"
        b"5a636465666768696a737475767778797a82838485868788898a"
        b"92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9"
        b"bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8"
        b"e9eaf2f3f4f5f6f7f8f9faffda000c03010002110311003f00fa"
        b"a68a28a00fffd9"
    ),
)

small_lossless_webp = TestImage(
    unhexlify(b"524946461a000000574542505650384c0d0000002f00000010071011118888fe0700"),
    b"image/webp",
    b".webp",
)

empty_file = TestImage(
    b"",
    b"image/gif",
    b".gif",
    expected_found=False,
    unable_to_thumbnail=True,
)

SVG = TestImage(
    b"""<?xml version="1.0"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN"
  "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">

<svg xmlns="http://www.w3.org/2000/svg"
     width="400" height="400">
  <circle cx="100" cy="100" r="50" stroke="black"
    stroke-width="5" fill="red" />
</svg>""",
    b"image/svg",
    b".svg",
    expected_found=False,
    unable_to_thumbnail=True,
    is_inline=False,
)
test_images = [
    small_png,
    small_png_with_transparency,
    small_lossless_webp,
    empty_file,
    SVG,
]
input_values = [(x,) for x in test_images]


@parameterized_class(("test_image",), input_values)
class MediaRepoTests(unittest.HomeserverTestCase):
    servlets = [media.register_servlets]
    test_image: ClassVar[TestImage]
    hijack_auth = True
    user_id = "@test:user"

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.fetches: list[
            tuple[
                "Deferred[tuple[bytes, tuple[int, dict[bytes, list[bytes]]]]]",
                str,
                str,
                QueryParams | None,
            ]
        ] = []

        def get_file(
            destination: str,
            path: str,
            output_stream: BinaryIO,
            download_ratelimiter: Ratelimiter,
            ip_address: Any,
            max_size: int,
            args: QueryParams | None = None,
            retry_on_dns_fail: bool = True,
            ignore_backoff: bool = False,
            follow_redirects: bool = False,
        ) -> "Deferred[tuple[int, dict[bytes, list[bytes]]]]":
            """A mock for MatrixFederationHttpClient.get_file."""

            def write_to(
                r: tuple[bytes, tuple[int, dict[bytes, list[bytes]]]],
            ) -> tuple[int, dict[bytes, list[bytes]]]:
                data, response = r
                output_stream.write(data)
                return response

            def write_err(f: Failure) -> Failure:
                f.trap(HttpResponseException)
                output_stream.write(f.value.response)
                return f

            d: Deferred[tuple[bytes, tuple[int, dict[bytes, list[bytes]]]]] = Deferred()
            self.fetches.append((d, destination, path, args))
            # Note that this callback changes the value held by d.
            d_after_callback = d.addCallbacks(write_to, write_err)
            return make_deferred_yieldable(d_after_callback)

        # Mock out the homeserver's MatrixFederationHttpClient
        client = Mock()
        client.get_file = get_file

        self.storage_path = self.mktemp()
        self.media_store_path = self.mktemp()
        os.mkdir(self.storage_path)
        os.mkdir(self.media_store_path)

        config = self.default_config()
        config["media_store_path"] = self.media_store_path
        config["max_image_pixels"] = 2000000

        provider_config = {
            "module": "synapse.media.storage_provider.FileStorageProviderBackend",
            "store_local": True,
            "store_synchronous": False,
            "store_remote": True,
            "config": {"directory": self.storage_path},
        }
        config["media_storage_providers"] = [provider_config]

        hs = self.setup_test_homeserver(config=config, federation_http_client=client)

        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.media_repo = hs.get_media_repository()

        self.media_id = "example.com/12345"

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def _req(
        self, content_disposition: bytes | None, include_content_type: bool = True
    ) -> FakeChannel:
        channel = self.make_request(
            "GET",
            f"/_matrix/media/v3/download/{self.media_id}",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        # We've made one fetch, to example.com, using the media URL, and asking
        # the other server not to do a remote fetch
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.fetches[0][1], "example.com")
        self.assertEqual(
            self.fetches[0][2], "/_matrix/media/v3/download/" + self.media_id
        )
        self.assertEqual(
            self.fetches[0][3],
            {"allow_remote": "false", "timeout_ms": "20000", "allow_redirect": "true"},
        )

        headers = {
            b"Content-Length": [b"%d" % (len(self.test_image.data))],
        }

        if include_content_type:
            headers[b"Content-Type"] = [self.test_image.content_type]

        if content_disposition:
            headers[b"Content-Disposition"] = [content_disposition]

        self.fetches[0][0].callback(
            (self.test_image.data, (len(self.test_image.data), headers))
        )

        self.pump()
        self.assertEqual(channel.code, 200)

        return channel

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_handle_missing_content_type(self) -> None:
        channel = self._req(
            b"attachment; filename=out" + self.test_image.extension,
            include_content_type=False,
        )
        headers = channel.headers
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            headers.getRawHeaders(b"Content-Type"), [b"application/octet-stream"]
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_disposition_filename_ascii(self) -> None:
        """
        If the filename is filename=<ascii> then Synapse will decode it as an
        ASCII string, and use filename= in the response.
        """
        channel = self._req(b"attachment; filename=out" + self.test_image.extension)

        headers = channel.headers
        self.assertEqual(
            headers.getRawHeaders(b"Content-Type"), [self.test_image.content_type]
        )
        self.assertEqual(
            headers.getRawHeaders(b"Content-Disposition"),
            [
                (b"inline" if self.test_image.is_inline else b"attachment")
                + b"; filename=out"
                + self.test_image.extension
            ],
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_disposition_filenamestar_utf8escaped(self) -> None:
        """
        If the filename is filename=*utf8''<utf8 escaped> then Synapse will
        correctly decode it as the UTF-8 string, and use filename* in the
        response.
        """
        filename = parse.quote("\u2603".encode()).encode("ascii")
        channel = self._req(
            b"attachment; filename*=utf-8''" + filename + self.test_image.extension
        )

        headers = channel.headers
        self.assertEqual(
            headers.getRawHeaders(b"Content-Type"), [self.test_image.content_type]
        )
        self.assertEqual(
            headers.getRawHeaders(b"Content-Disposition"),
            [
                (b"inline" if self.test_image.is_inline else b"attachment")
                + b"; filename*=utf-8''"
                + filename
                + self.test_image.extension
            ],
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_disposition_none(self) -> None:
        """
        If there is no filename, Content-Disposition should only
        be a disposition type.
        """
        channel = self._req(None)

        headers = channel.headers
        self.assertEqual(
            headers.getRawHeaders(b"Content-Type"), [self.test_image.content_type]
        )
        self.assertEqual(
            headers.getRawHeaders(b"Content-Disposition"),
            [b"inline" if self.test_image.is_inline else b"attachment"],
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_thumbnail_crop(self) -> None:
        """Test that a cropped remote thumbnail is available."""
        self._test_thumbnail(
            "crop",
            self.test_image.expected_cropped,
            expected_found=self.test_image.expected_found,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_thumbnail_scale(self) -> None:
        """Test that a scaled remote thumbnail is available."""
        self._test_thumbnail(
            "scale",
            self.test_image.expected_scaled,
            expected_found=self.test_image.expected_found,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_invalid_type(self) -> None:
        """An invalid thumbnail type is never available."""
        self._test_thumbnail(
            "invalid",
            None,
            expected_found=False,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

    @unittest.override_config(
        {
            "thumbnail_sizes": [{"width": 32, "height": 32, "method": "scale"}],
            "enable_authenticated_media": False,
        },
    )
    def test_no_thumbnail_crop(self) -> None:
        """
        Override the config to generate only scaled thumbnails, but request a cropped one.
        """
        self._test_thumbnail(
            "crop",
            None,
            expected_found=False,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

    @unittest.override_config(
        {
            "thumbnail_sizes": [{"width": 32, "height": 32, "method": "crop"}],
            "enable_authenticated_media": False,
        }
    )
    def test_no_thumbnail_scale(self) -> None:
        """
        Override the config to generate only cropped thumbnails, but request a scaled one.
        """
        self._test_thumbnail(
            "scale",
            None,
            expected_found=False,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_thumbnail_repeated_thumbnail(self) -> None:
        """Test that fetching the same thumbnail works, and deleting the on disk
        thumbnail regenerates it.
        """
        self._test_thumbnail(
            "scale",
            self.test_image.expected_scaled,
            expected_found=self.test_image.expected_found,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

        if not self.test_image.expected_found:
            return

        # Fetching again should work, without re-requesting the image from the
        # remote.
        params = "?width=32&height=32&method=scale"
        channel = self.make_request(
            "GET",
            f"/_matrix/media/r0/thumbnail/{self.media_id}{params}",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        self.assertEqual(channel.code, 200)
        if self.test_image.expected_scaled:
            self.assertEqual(
                channel.result["body"],
                self.test_image.expected_scaled,
                channel.result["body"],
            )

        # Deleting the thumbnail on disk then re-requesting it should work as
        # Synapse should regenerate missing thumbnails.
        origin, media_id = self.media_id.split("/")
        info = self.get_success(self.store.get_cached_remote_media(origin, media_id))
        assert info is not None
        file_id = info.filesystem_id

        thumbnail_dir = self.media_repo.filepaths.remote_media_thumbnail_dir(
            origin, file_id
        )
        shutil.rmtree(thumbnail_dir, ignore_errors=True)

        channel = self.make_request(
            "GET",
            f"/_matrix/media/r0/thumbnail/{self.media_id}{params}",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        self.assertEqual(channel.code, 200)
        if self.test_image.expected_scaled:
            self.assertEqual(
                channel.result["body"],
                self.test_image.expected_scaled,
                channel.result["body"],
            )

    def _test_thumbnail(
        self,
        method: str,
        expected_body: bytes | None,
        expected_found: bool,
        unable_to_thumbnail: bool = False,
    ) -> None:
        """Test the given thumbnailing method works as expected.

        Args:
            method: The thumbnailing method to use (crop, scale).
            expected_body: The expected bytes from thumbnailing, or None if
                test should just check for a valid image.
            expected_found: True if the file should exist on the server, or False if
                a 404/400 is expected.
            unable_to_thumbnail: True if we expect the thumbnailing to fail (400), or
                False if the thumbnailing should succeed or a normal 404 is expected.
        """

        params = "?width=32&height=32&method=" + method
        channel = self.make_request(
            "GET",
            f"/_matrix/media/r0/thumbnail/{self.media_id}{params}",
            shorthand=False,
            await_result=False,
        )
        self.pump()
        headers = {
            b"Content-Length": [b"%d" % (len(self.test_image.data))],
            b"Content-Type": [self.test_image.content_type],
        }
        self.fetches[0][0].callback(
            (self.test_image.data, (len(self.test_image.data), headers))
        )
        self.pump()
        if expected_found:
            self.assertEqual(channel.code, 200)

            self.assertEqual(
                channel.headers.getRawHeaders(b"Cross-Origin-Resource-Policy"),
                [b"cross-origin"],
            )

            if expected_body is not None:
                self.assertEqual(
                    channel.result["body"], expected_body, channel.result["body"]
                )
            else:
                # ensure that the result is at least some valid image
                Image.open(BytesIO(channel.result["body"]))
        elif unable_to_thumbnail:
            # A 400 with a JSON body.
            self.assertEqual(channel.code, 400)
            self.assertEqual(
                channel.json_body,
                {
                    "errcode": "M_UNKNOWN",
                    "error": "Cannot find any thumbnails for the requested media ('/_matrix/media/r0/thumbnail/example.com/12345'). This might mean the media is not a supported_media_format=(image/jpeg, image/jpg, image/webp, image/gif, image/png) or that thumbnailing failed for some other reason. (Dynamic thumbnails are disabled on this server.)",
                },
            )
        else:
            # A 404 with a JSON body.
            self.assertEqual(channel.code, 404)
            self.assertEqual(
                channel.json_body,
                {
                    "errcode": "M_NOT_FOUND",
                    "error": "Not found '/_matrix/media/r0/thumbnail/example.com/12345'",
                },
            )

    @parameterized.expand([("crop", 16), ("crop", 64), ("scale", 16), ("scale", 64)])
    def test_same_quality(self, method: str, desired_size: int) -> None:
        """Test that choosing between thumbnails with the same quality rating succeeds.

        We are not particular about which thumbnail is chosen."""

        content_type = self.test_image.content_type.decode()
        media_repo = self.hs.get_media_repository()
        thumbnail_provider = ThumbnailProvider(
            self.hs, media_repo, media_repo.media_storage
        )

        self.assertIsNotNone(
            thumbnail_provider._select_thumbnail(
                desired_width=desired_size,
                desired_height=desired_size,
                desired_method=method,
                desired_type=content_type,
                # Provide two identical thumbnails which are guaranteed to have the same
                # quality rating.
                thumbnail_infos=[
                    ThumbnailInfo(
                        width=32,
                        height=32,
                        method=method,
                        type=content_type,
                        length=256,
                    ),
                    ThumbnailInfo(
                        width=32,
                        height=32,
                        method=method,
                        type=content_type,
                        length=256,
                    ),
                ],
                file_id=f"image{self.test_image.extension.decode()}",
                url_cache=False,
                server_name=None,
            )
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_x_robots_tag_header(self) -> None:
        """
        Tests that the `X-Robots-Tag` header is present, which informs web crawlers
        to not index, archive, or follow links in media.
        """
        channel = self._req(b"attachment; filename=out" + self.test_image.extension)

        headers = channel.headers
        self.assertEqual(
            headers.getRawHeaders(b"X-Robots-Tag"),
            [b"noindex, nofollow, noarchive, noimageindex"],
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_cross_origin_resource_policy_header(self) -> None:
        """
        Test that the Cross-Origin-Resource-Policy header is set to "cross-origin"
        allowing web clients to embed media from the downloads API.
        """
        channel = self._req(b"attachment; filename=out" + self.test_image.extension)

        headers = channel.headers

        self.assertEqual(
            headers.getRawHeaders(b"Cross-Origin-Resource-Policy"),
            [b"cross-origin"],
        )

    @unittest.override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    def test_unknown_v3_endpoint(self) -> None:
        """
        If the v3 endpoint fails, try the r0 one.
        """
        channel = self.make_request(
            "GET",
            f"/_matrix/media/v3/download/{self.media_id}",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        # We've made one fetch, to example.com, using the media URL, and asking
        # the other server not to do a remote fetch
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.fetches[0][1], "example.com")
        self.assertEqual(
            self.fetches[0][2], "/_matrix/media/v3/download/" + self.media_id
        )

        # The result which says the endpoint is unknown.
        unknown_endpoint = b'{"errcode":"M_UNRECOGNIZED","error":"Unknown request"}'
        self.fetches[0][0].errback(
            HttpResponseException(404, "NOT FOUND", unknown_endpoint)
        )

        self.pump()

        # There should now be another request to the r0 URL.
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.fetches[1][1], "example.com")
        self.assertEqual(
            self.fetches[1][2], f"/_matrix/media/r0/download/{self.media_id}"
        )

        headers = {
            b"Content-Length": [b"%d" % (len(self.test_image.data))],
        }

        self.fetches[1][0].callback(
            (self.test_image.data, (len(self.test_image.data), headers))
        )

        self.pump()
        self.assertEqual(channel.code, 200)


class TestSpamCheckerLegacy:
    """A spam checker module that rejects all media that includes the bytes
    `evil`.

    Uses the legacy Spam-Checker API.
    """

    def __init__(self, config: dict[str, Any], api: ModuleApi) -> None:
        self.config = config
        self.api = api

    @staticmethod
    def parse_config(config: dict[str, Any]) -> dict[str, Any]:
        return config

    async def check_event_for_spam(self, event: EventBase) -> bool | str:
        return False  # allow all events

    async def user_may_invite(
        self,
        inviter_userid: str,
        invitee_userid: str,
        room_id: str,
    ) -> bool:
        return True  # allow all invites

    async def user_may_create_room(self, userid: str) -> bool:
        return True  # allow all room creations

    async def user_may_create_room_alias(
        self, userid: str, room_alias: RoomAlias
    ) -> bool:
        return True  # allow all room aliases

    async def user_may_publish_room(self, userid: str, room_id: str) -> bool:
        return True  # allow publishing of all rooms

    async def check_media_file_for_spam(
        self, file_wrapper: ReadableFileWrapper, file_info: FileInfo
    ) -> bool:
        buf = BytesIO()
        await file_wrapper.write_chunks_to(buf.write)

        return b"evil" in buf.getvalue()


class SpamCheckerTestCaseLegacy(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        admin.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

        load_legacy_spam_checkers(hs)

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def default_config(self) -> dict[str, Any]:
        config = default_config("test")

        config.update(
            {
                "spam_checker": [
                    {
                        "module": TestSpamCheckerLegacy.__module__
                        + ".TestSpamCheckerLegacy",
                        "config": {},
                    }
                ]
            }
        )

        return config

    def test_upload_innocent(self) -> None:
        """Attempt to upload some innocent data that should be allowed."""
        self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=200)

    def test_upload_ban(self) -> None:
        """Attempt to upload some data that includes bytes "evil", which should
        get rejected by the spam checker.
        """

        data = b"Some evil data"

        self.helper.upload_media(data, tok=self.tok, expect_code=400)


EVIL_DATA = b"Some evil data"
EVIL_DATA_EXPERIMENT = b"Some evil data to trigger the experimental tuple API"


class SpamCheckerTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        admin.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

        hs.get_module_api().register_spam_checker_callbacks(
            check_media_file_for_spam=self.check_media_file_for_spam
        )

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    async def check_media_file_for_spam(
        self, file_wrapper: ReadableFileWrapper, file_info: FileInfo
    ) -> Codes | Literal["NOT_SPAM"] | tuple[Codes, JsonDict]:
        buf = BytesIO()
        await file_wrapper.write_chunks_to(buf.write)

        if buf.getvalue() == EVIL_DATA:
            return Codes.FORBIDDEN
        elif buf.getvalue() == EVIL_DATA_EXPERIMENT:
            return (Codes.FORBIDDEN, {})
        else:
            return "NOT_SPAM"

    def test_upload_innocent(self) -> None:
        """Attempt to upload some innocent data that should be allowed."""
        self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=200)

    def test_upload_ban(self) -> None:
        """Attempt to upload some data that includes bytes "evil", which should
        get rejected by the spam checker.
        """

        self.helper.upload_media(EVIL_DATA, tok=self.tok, expect_code=400)

        self.helper.upload_media(
            EVIL_DATA_EXPERIMENT,
            tok=self.tok,
            expect_code=400,
        )


class RemoteDownloadLimiterTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

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
        self.repo = hs.get_media_repository()
        self.client = hs.get_federation_http_client()
        self.store = hs.get_datastores().main

    def create_resource_dict(self) -> dict[str, Resource]:
        # We need to manually set the resource tree to include media, the
        # default only does `/_matrix/client` APIs.
        return {"/_matrix/media": self.hs.get_media_repository_resource()}

    # mock actually reading file body
    def read_body_with_max_size_30MiB(*args: Any, **kwargs: Any) -> Deferred:
        d: Deferred = defer.Deferred()
        d.callback(31457280)
        return d

    def read_body_with_max_size_50MiB(*args: Any, **kwargs: Any) -> Deferred:
        d: Deferred = defer.Deferred()
        d.callback(52428800)
        return d

    @override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    @patch(
        "synapse.http.matrixfederationclient.read_body_with_max_size",
        read_body_with_max_size_30MiB,
    )
    def test_download_ratelimit_default(self) -> None:
        """
        Test remote media download ratelimiting against default configuration - 500MB bucket
        and 87kb/second drain rate
        """

        # mock out actually sending the request, returns a 30MiB response
        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = 31457280
            resp.headers = Headers({"Content-Type": ["application/octet-stream"]})
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # first request should go through
        channel = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxyz",
            shorthand=False,
        )
        assert channel.code == 200

        # next 15 should go through
        for i in range(15):
            channel2 = self.make_request(
                "GET",
                f"/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxy{i}",
                shorthand=False,
            )
            assert channel2.code == 200

        # 17th will hit ratelimit
        channel3 = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxyx",
            shorthand=False,
        )
        assert channel3.code == 429

        # however, a request from a different IP will go through
        channel4 = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxyz",
            shorthand=False,
            client_ip="187.233.230.159",
        )
        assert channel4.code == 200

        # at 87Kib/s it should take about 2 minutes for enough to drain from bucket that another
        # 30MiB download is authorized - The last download was blocked at 503,316,480.
        # The next download will be authorized when bucket hits 492,830,720
        # (524,288,000 total capacity - 31,457,280 download size) so 503,316,480 - 492,830,720 ~= 10,485,760
        # needs to drain before another download will be authorized, that will take ~=
        # 2 minutes (10,485,760/89,088/60)
        self.reactor.pump([2.0 * 60.0])

        # enough has drained and next request goes through
        channel5 = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxyb",
            shorthand=False,
        )
        assert channel5.code == 200

    @override_config(
        {
            "remote_media_download_per_second": "50M",
            "remote_media_download_burst_count": "50M",
            "enable_authenticated_media": False,
        }
    )
    @patch(
        "synapse.http.matrixfederationclient.read_body_with_max_size",
        read_body_with_max_size_50MiB,
    )
    def test_download_rate_limit_config(self) -> None:
        """
        Test that download rate limit config options are correctly picked up and applied
        """

        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = 52428800
            resp.headers = Headers({"Content-Type": ["application/octet-stream"]})
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # first request should go through
        channel = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxyz",
            shorthand=False,
        )
        assert channel.code == 200

        # immediate second request should fail
        channel = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxy1",
            shorthand=False,
        )
        assert channel.code == 429

        # advance half a second
        self.reactor.pump([0.5])

        # request still fails
        channel = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxy2",
            shorthand=False,
        )
        assert channel.code == 429

        # advance another half second
        self.reactor.pump([0.5])

        # enough has drained from bucket and request is successful
        channel = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxy3",
            shorthand=False,
        )
        assert channel.code == 200

    @override_config(
        {
            "remote_media_download_burst_count": "87M",
            "enable_authenticated_media": False,
        }
    )
    @patch(
        "synapse.http.matrixfederationclient.read_body_with_max_size",
        read_body_with_max_size_30MiB,
    )
    def test_download_ratelimit_unknown_length(self) -> None:
        """
        Test that if no content-length is provided, ratelimit will still be applied after
        download once length is known
        """

        # mock out actually sending the request
        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = UNKNOWN_LENGTH
            resp.headers = Headers({"Content-Type": ["application/octet-stream"]})
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # 3 requests should go through (note 3rd one would technically violate ratelimit but
        # is applied *after* download - the next one will be ratelimited)
        for i in range(3):
            channel = self.make_request(
                "GET",
                f"/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxy{i}",
                shorthand=False,
            )
            assert channel.code == 200

        # 4th will hit ratelimit
        channel2 = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abcdefghijklmnopqrstuvwxyx",
            shorthand=False,
        )
        assert channel2.code == 429

    @override_config({"max_upload_size": "29M", "enable_authenticated_media": False})
    @patch(
        "synapse.http.matrixfederationclient.read_body_with_max_size",
        read_body_with_max_size_30MiB,
    )
    def test_max_download_respected(self) -> None:
        """
        Test that the max download size is enforced - note that max download size is determined
        by the max_upload_size
        """

        # mock out actually sending the request
        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = 31457280
            resp.headers = Headers({"Content-Type": ["application/octet-stream"]})
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        channel = self.make_request(
            "GET", "/_matrix/media/v3/download/remote.org/abcd", shorthand=False
        )
        assert channel.code == 502
        assert channel.json_body["errcode"] == "M_TOO_LARGE"


def read_body(
    response: IResponse, stream: ByteWriteable, max_size: int | None
) -> Deferred:
    d: Deferred = defer.Deferred()
    stream.write(SMALL_PNG)
    d.callback(len(SMALL_PNG))
    return d


class MediaHashesTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        media.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")
        self.store = hs.get_datastores().main
        self.client = hs.get_federation_http_client()

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def test_ensure_correct_sha256(self) -> None:
        """Check that the hash does not change"""
        media = self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=200)
        mxc = media.get("content_uri")
        assert mxc
        store_media = self.get_success(self.store.get_local_media(mxc[11:]))
        assert store_media
        self.assertEqual(
            store_media.sha256,
            SMALL_PNG_SHA256,
        )

    def test_ensure_multiple_correct_sha256(self) -> None:
        """Check that two media items have the same hash."""
        media_a = self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=200)
        mxc_a = media_a.get("content_uri")
        assert mxc_a
        store_media_a = self.get_success(self.store.get_local_media(mxc_a[11:]))
        assert store_media_a

        media_b = self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=200)
        mxc_b = media_b.get("content_uri")
        assert mxc_b
        store_media_b = self.get_success(self.store.get_local_media(mxc_b[11:]))
        assert store_media_b

        self.assertNotEqual(
            store_media_a.media_id,
            store_media_b.media_id,
        )
        self.assertEqual(
            store_media_a.sha256,
            store_media_b.sha256,
        )

    @override_config(
        {
            "enable_authenticated_media": False,
        }
    )
    # mock actually reading file body
    @patch(
        "synapse.http.matrixfederationclient.read_body_with_max_size",
        read_body,
    )
    def test_ensure_correct_sha256_federated(self) -> None:
        """Check that federated media have the same hash."""

        # Mock getting a file over federation
        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = 500
            resp.headers = Headers({"Content-Type": ["application/octet-stream"]})
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # first request should go through
        channel = self.make_request(
            "GET",
            "/_matrix/media/v3/download/remote.org/abc",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 200
        store_media = self.get_success(
            self.store.get_cached_remote_media("remote.org", "abc")
        )
        assert store_media
        self.assertEqual(
            store_media.sha256,
            SMALL_PNG_SHA256,
        )


class MediaRepoSizeModuleCallbackTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        admin.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")
        self.mock_result = True  # Allow all uploads by default

        hs.get_module_api().register_media_repository_callbacks(
            is_user_allowed_to_upload_media_of_size=self.is_user_allowed_to_upload_media_of_size,
        )

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    async def is_user_allowed_to_upload_media_of_size(
        self, user_id: str, size: int
    ) -> bool:
        self.last_user_id = user_id
        self.last_size = size
        return self.mock_result

    def test_upload_allowed(self) -> None:
        self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=200)
        assert self.last_user_id == self.user
        assert self.last_size == len(SMALL_PNG)

    def test_upload_not_allowed(self) -> None:
        self.mock_result = False
        self.helper.upload_media(SMALL_PNG, tok=self.tok, expect_code=413)
        assert self.last_user_id == self.user
        assert self.last_size == len(SMALL_PNG)
