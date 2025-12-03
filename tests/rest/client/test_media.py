#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
import base64
import io
import json
import os
import re
import shutil
from typing import Any, BinaryIO, ClassVar, Sequence
from unittest.mock import MagicMock, Mock, patch
from urllib import parse
from urllib.parse import quote, urlencode

from parameterized import parameterized, parameterized_class
from PIL import Image as Image

from twisted.internet import defer
from twisted.internet._resolver import HostResolution
from twisted.internet.address import IPv4Address, IPv6Address
from twisted.internet.defer import Deferred
from twisted.internet.error import DNSLookupError
from twisted.internet.interfaces import IAddress, IResolutionReceiver
from twisted.internet.testing import AccumulatingProtocol, MemoryReactor
from twisted.python.failure import Failure
from twisted.web.http_headers import Headers
from twisted.web.iweb import UNKNOWN_LENGTH, IResponse
from twisted.web.resource import Resource

from synapse.api.errors import HttpResponseException
from synapse.api.ratelimiting import Ratelimiter
from synapse.config._base import Config
from synapse.config.oembed import OEmbedEndpointConfig
from synapse.http.client import MultipartResponse
from synapse.http.types import QueryParams
from synapse.logging.context import make_deferred_yieldable
from synapse.media._base import FileInfo, ThumbnailInfo
from synapse.media.thumbnailer import ThumbnailProvider
from synapse.media.url_previewer import IMAGE_CACHE_EXPIRY_MS
from synapse.module_api import MediaUploadLimit
from synapse.rest import admin
from synapse.rest.client import login, media
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock
from synapse.util.stringutils import parse_and_validate_mxc_uri

from tests import unittest
from tests.media.test_media_storage import (
    SVG,
    TestImage,
    empty_file,
    small_cmyk_jpeg,
    small_lossless_webp,
    small_png,
    small_png_with_transparency,
)
from tests.server import FakeChannel, FakeTransport, ThreadedMemoryReactorClock
from tests.test_utils import SMALL_PNG
from tests.unittest import override_config

try:
    import lxml
except ImportError:
    lxml = None  # type: ignore[assignment]


class MediaDomainBlockingTests(unittest.HomeserverTestCase):
    remote_media_id = "doesnotmatter"
    remote_server_name = "evil.com"
    servlets = [
        media.register_servlets,
        admin.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(
        self, reactor: ThreadedMemoryReactorClock, clock: Clock
    ) -> HomeServer:
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
        self.store = hs.get_datastores().main

        # Inject a piece of media. We'll use this to ensure we're returning a sane
        # response when we're not supposed to block it, distinguishing a media block
        # from a regular 404.
        file_id = "abcdefg12345"
        file_info = FileInfo(server_name=self.remote_server_name, file_id=file_id)

        media_storage = hs.get_media_repository().media_storage

        ctx = media_storage.store_into_file(file_info)
        (f, fname) = self.get_success(ctx.__aenter__())
        f.write(SMALL_PNG)
        self.get_success(ctx.__aexit__(None, None, None))

        self.get_success(
            self.store.store_cached_remote_media(
                origin=self.remote_server_name,
                media_id=self.remote_media_id,
                media_type="image/png",
                media_length=1,
                time_now_ms=clock.time_msec(),
                upload_name="test.png",
                filesystem_id=file_id,
                sha256=file_id,
            )
        )
        self.register_user("user", "password")
        self.tok = self.login("user", "password")

    @override_config(
        {
            # Disable downloads from the domain we'll be trying to download from.
            # Should result in a 404.
            "prevent_media_downloads_from": ["evil.com"],
            "dynamic_thumbnails": True,
        }
    )
    def test_cannot_download_blocked_media_thumbnail(self) -> None:
        """
        Same test as test_cannot_download_blocked_media but for thumbnails.
        """
        response = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/evil.com/{self.remote_media_id}?width=100&height=100",
            shorthand=False,
            content={"width": 100, "height": 100},
            access_token=self.tok,
        )
        self.assertEqual(response.code, 404)

    @override_config(
        {
            # Disable downloads from a domain we won't be requesting downloads from.
            # This proves we haven't broken anything.
            "prevent_media_downloads_from": ["not-listed.com"],
            "dynamic_thumbnails": True,
        }
    )
    def test_remote_media_thumbnail_normally_unblocked(self) -> None:
        """
        Same test as test_remote_media_normally_unblocked but for thumbnails.
        """
        response = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/evil.com/{self.remote_media_id}?width=100&height=100",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(response.code, 200)


class URLPreviewTests(unittest.HomeserverTestCase):
    if not lxml:
        skip = "url preview feature requires lxml"

    servlets = [media.register_servlets]
    hijack_auth = True
    user_id = "@test:user"
    end_content = (
        b"<html><head>"
        b'<meta property="og:title" content="~matrix~" />'
        b'<meta property="og:description" content="hi" />'
        b"</head></html>"
    )

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
        config["url_preview_ip_range_whitelist"] = ("1.1.1.1",)
        config["url_preview_accept_language"] = [
            "en-UK",
            "en-US;q=0.9",
            "fr;q=0.8",
            "*;q=0.7",
        ]

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

        hs = self.setup_test_homeserver(config=config)

        # After the hs is created, modify the parsed oEmbed config (to avoid
        # messing with files).
        #
        # Note that HTTP URLs are used to avoid having to deal with TLS in tests.
        hs.config.oembed.oembed_patterns = [
            OEmbedEndpointConfig(
                api_endpoint="http://publish.twitter.com/oembed",
                url_patterns=[
                    re.compile(r"http://twitter\.com/.+/status/.+"),
                ],
                formats=None,
            ),
            OEmbedEndpointConfig(
                api_endpoint="http://www.hulu.com/api/oembed.{format}",
                url_patterns=[
                    re.compile(r"http://www\.hulu\.com/watch/.+"),
                ],
                formats=["json"],
            ),
        ]

        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.media_repo = hs.get_media_repository()
        assert self.media_repo.url_previewer is not None
        self.url_previewer = self.media_repo.url_previewer

        self.lookups: dict[str, Any] = {}

        class Resolver:
            def resolveHostName(
                _self,
                resolutionReceiver: IResolutionReceiver,
                hostName: str,
                portNumber: int = 0,
                addressTypes: Sequence[type[IAddress]] | None = None,
                transportSemantics: str = "TCP",
            ) -> IResolutionReceiver:
                resolution = HostResolution(hostName)
                resolutionReceiver.resolutionBegan(resolution)
                if hostName not in self.lookups:
                    raise DNSLookupError("OH NO")

                for i in self.lookups[hostName]:
                    resolutionReceiver.addressResolved(i[0]("TCP", i[1], portNumber))
                resolutionReceiver.resolutionComplete()
                return resolutionReceiver

        self.reactor.nameResolver = Resolver()  # type: ignore[assignment]

    def _assert_small_png(self, json_body: JsonDict) -> None:
        """Assert properties from the SMALL_PNG test image."""
        self.assertTrue(json_body["og:image"].startswith("mxc://"))
        self.assertEqual(json_body["og:image:height"], 1)
        self.assertEqual(json_body["og:image:width"], 1)
        self.assertEqual(json_body["og:image:type"], "image/png")
        self.assertEqual(json_body["matrix:image:size"], 67)

    def test_cache_returns_correct_type(self) -> None:
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\nContent-Type: text/html\r\n\r\n"
            % (len(self.end_content),)
            + self.end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"og:title": "~matrix~", "og:description": "hi"}
        )

        # Check the cache returns the correct response
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
        )

        # Check the cache response has the same content
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"og:title": "~matrix~", "og:description": "hi"}
        )

        # Clear the in-memory cache
        self.assertIn("http://matrix.org", self.url_previewer._cache)
        self.url_previewer._cache.pop("http://matrix.org")
        self.assertNotIn("http://matrix.org", self.url_previewer._cache)

        # Check the database cache returns the correct response
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
        )

        # Check the cache response has the same content
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"og:title": "~matrix~", "og:description": "hi"}
        )

    def test_non_ascii_preview_httpequiv(self) -> None:
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        end_content = (
            b"<html><head>"
            b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1251"/>'
            b'<meta property="og:title" content="\xe4\xea\xe0" />'
            b'<meta property="og:description" content="hi" />'
            b"</head></html>"
        )

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(end_content),)
            + end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["og:title"], "\u0434\u043a\u0430")

    def test_video_rejected(self) -> None:
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        end_content = b"anything"

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b"Content-Type: video/mp4\r\n\r\n"
            )
            % (len(end_content))
            + end_content
        )

        self.pump()
        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "Requested file's content type not allowed for this operation: video/mp4",
            },
        )

    def test_audio_rejected(self) -> None:
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        end_content = b"anything"

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b"Content-Type: audio/aac\r\n\r\n"
            )
            % (len(end_content))
            + end_content
        )

        self.pump()
        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "Requested file's content type not allowed for this operation: audio/aac",
            },
        )

    def test_non_ascii_preview_content_type(self) -> None:
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        end_content = (
            b"<html><head>"
            b'<meta property="og:title" content="\xe4\xea\xe0" />'
            b'<meta property="og:description" content="hi" />'
            b"</head></html>"
        )

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="windows-1251"\r\n\r\n'
            )
            % (len(end_content),)
            + end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["og:title"], "\u0434\u043a\u0430")

    def test_overlong_title(self) -> None:
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        end_content = (
            b"<html><head>"
            b"<title>" + b"x" * 2000 + b"</title>"
            b'<meta property="og:description" content="hi" />'
            b"</head></html>"
        )

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="windows-1251"\r\n\r\n'
            )
            % (len(end_content),)
            + end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        res = channel.json_body
        # We should only see the `og:description` field, as `title` is too long and should be stripped out
        self.assertCountEqual(["og:description"], res.keys())

    def test_ipaddr(self) -> None:
        """
        IP addresses can be previewed directly.
        """
        self.lookups["example.com"] = [(IPv4Address, "10.1.2.3")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\nContent-Type: text/html\r\n\r\n"
            % (len(self.end_content),)
            + self.end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"og:title": "~matrix~", "og:description": "hi"}
        )

    def test_blocked_ip_specific(self) -> None:
        """
        Blocked IP addresses, found via DNS, are not spidered.
        """
        self.lookups["example.com"] = [(IPv4Address, "192.168.1.1")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
        )

        # No requests made.
        self.assertEqual(len(self.reactor.tcpClients), 0)
        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "DNS resolution failure during URL preview generation",
            },
        )

    def test_blocked_ip_range(self) -> None:
        """
        Blocked IP ranges, IPs found over DNS, are not spidered.
        """
        self.lookups["example.com"] = [(IPv4Address, "1.1.1.2")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
        )

        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "DNS resolution failure during URL preview generation",
            },
        )

    def test_blocked_ip_specific_direct(self) -> None:
        """
        Blocked IP addresses, accessed directly, are not spidered.
        """
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://192.168.1.1",
            shorthand=False,
        )

        # No requests made.
        self.assertEqual(len(self.reactor.tcpClients), 0)
        self.assertEqual(
            channel.json_body,
            {"errcode": "M_UNKNOWN", "error": "IP address blocked"},
        )
        self.assertEqual(channel.code, 403)

    def test_blocked_ip_range_direct(self) -> None:
        """
        Blocked IP ranges, accessed directly, are not spidered.
        """
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://1.1.1.2",
            shorthand=False,
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(
            channel.json_body,
            {"errcode": "M_UNKNOWN", "error": "IP address blocked"},
        )

    def test_blocked_ip_range_whitelisted_ip(self) -> None:
        """
        Blocked but then subsequently whitelisted IP addresses can be
        spidered.
        """
        self.lookups["example.com"] = [(IPv4Address, "1.1.1.1")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)

        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))

        client.dataReceived(
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\nContent-Type: text/html\r\n\r\n"
            % (len(self.end_content),)
            + self.end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"og:title": "~matrix~", "og:description": "hi"}
        )

    def test_blocked_ip_with_external_ip(self) -> None:
        """
        If a hostname resolves a blocked IP, even if there's a non-blocked one,
        it will be rejected.
        """
        # Hardcode the URL resolving to the IP we want.
        self.lookups["example.com"] = [
            (IPv4Address, "1.1.1.2"),
            (IPv4Address, "10.1.2.3"),
        ]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
        )
        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "DNS resolution failure during URL preview generation",
            },
        )

    def test_blocked_ipv6_specific(self) -> None:
        """
        Blocked IP addresses, found via DNS, are not spidered.
        """
        self.lookups["example.com"] = [
            (IPv6Address, "3fff:ffff:ffff:ffff:ffff:ffff:ffff:ffff")
        ]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
        )

        # No requests made.
        self.assertEqual(len(self.reactor.tcpClients), 0)
        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "DNS resolution failure during URL preview generation",
            },
        )

    def test_blocked_ipv6_range(self) -> None:
        """
        Blocked IP ranges, IPs found over DNS, are not spidered.
        """
        self.lookups["example.com"] = [(IPv6Address, "2001:800::1")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
        )

        self.assertEqual(channel.code, 502)
        self.assertEqual(
            channel.json_body,
            {
                "errcode": "M_UNKNOWN",
                "error": "DNS resolution failure during URL preview generation",
            },
        )

    def test_OPTIONS(self) -> None:
        """
        OPTIONS returns the OPTIONS.
        """
        channel = self.make_request(
            "OPTIONS",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
        )
        self.assertEqual(channel.code, 204)

    def test_accept_language_config_option(self) -> None:
        """
        Accept-Language header is sent to the remote server
        """
        self.lookups["example.com"] = [(IPv4Address, "10.1.2.3")]

        # Build and make a request to the server
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://example.com",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        # Extract Synapse's tcp client
        client = self.reactor.tcpClients[0][2].buildProtocol(None)

        # Build a fake remote server to reply with
        server = AccumulatingProtocol()

        # Connect the two together
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))

        # Tell Synapse that it has received some data from the remote server
        client.dataReceived(
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\nContent-Type: text/html\r\n\r\n"
            % (len(self.end_content),)
            + self.end_content
        )

        # Move the reactor along until we get a response on our original channel
        self.pump()
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body, {"og:title": "~matrix~", "og:description": "hi"}
        )

        # Check that the server received the Accept-Language header as part
        # of the request from Synapse
        self.assertIn(
            (
                b"Accept-Language: en-UK\r\n"
                b"Accept-Language: en-US;q=0.9\r\n"
                b"Accept-Language: fr;q=0.8\r\n"
                b"Accept-Language: *;q=0.7"
            ),
            server.data,
        )

    def test_image(self) -> None:
        """An image should be precached if mentioned in the HTML."""
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]
        self.lookups["cdn.matrix.org"] = [(IPv4Address, "10.1.2.4")]

        result = (
            b"""<html><body><img src="http://cdn.matrix.org/foo.png"></body></html>"""
        )

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        # Respond with the HTML.
        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(result),)
            + result
        )
        self.pump()

        # Respond with the photo.
        client = self.reactor.tcpClients[1][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b"Content-Type: image/png\r\n\r\n"
            )
            % (len(SMALL_PNG),)
            + SMALL_PNG
        )
        self.pump()

        # The image should be in the result.
        self.assertEqual(channel.code, 200)
        self._assert_small_png(channel.json_body)

    def test_nonexistent_image(self) -> None:
        """If the preview image doesn't exist, ensure some data is returned."""
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        result = (
            b"""<html><body><img src="http://cdn.matrix.org/foo.jpg"></body></html>"""
        )

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(result),)
            + result
        )

        self.pump()

        # There should not be a second connection.
        self.assertEqual(len(self.reactor.tcpClients), 1)

        # The image should not be in the result.
        self.assertEqual(channel.code, 200)
        self.assertNotIn("og:image", channel.json_body)

    @unittest.override_config(
        {"url_preview_url_blacklist": [{"netloc": "cdn.matrix.org"}]}
    )
    def test_image_blocked(self) -> None:
        """If the preview image doesn't exist, ensure some data is returned."""
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]
        self.lookups["cdn.matrix.org"] = [(IPv4Address, "10.1.2.4")]

        result = (
            b"""<html><body><img src="http://cdn.matrix.org/foo.jpg"></body></html>"""
        )

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(result),)
            + result
        )
        self.pump()

        # There should not be a second connection.
        self.assertEqual(len(self.reactor.tcpClients), 1)

        # The image should not be in the result.
        self.assertEqual(channel.code, 200)
        self.assertNotIn("og:image", channel.json_body)

    def test_oembed_failure(self) -> None:
        """If the autodiscovered oEmbed URL fails, ensure some data is returned."""
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        result = b"""
        <title>oEmbed Autodiscovery Fail</title>
        <link rel="alternate" type="application/json+oembed"
            href="http://example.com/oembed?url=http%3A%2F%2Fmatrix.org&format=json"
            title="matrixdotorg" />
        """

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(result),)
            + result
        )

        self.pump()
        self.assertEqual(channel.code, 200)

        # The image should not be in the result.
        self.assertEqual(channel.json_body["og:title"], "oEmbed Autodiscovery Fail")

    def test_data_url(self) -> None:
        """
        Requesting to preview a data URL is not supported.
        """
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        data = base64.b64encode(SMALL_PNG).decode()

        query_params = urlencode(
            {
                "url": f'<html><head><img src="data:image/png;base64,{data}" /></head></html>'
            }
        )

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/preview_url?{query_params}",
            shorthand=False,
        )
        self.pump()

        self.assertEqual(channel.code, 500)

    def test_inline_data_url(self) -> None:
        """
        An inline image (as a data URL) should be parsed properly.
        """
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        data = base64.b64encode(SMALL_PNG)

        end_content = (
            b'<html><head><img src="data:image/png;base64,%s" /></head></html>'
        ) % (data,)

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://matrix.org",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(end_content),)
            + end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        self._assert_small_png(channel.json_body)

    def test_oembed_photo(self) -> None:
        """Test an oEmbed endpoint which returns a 'photo' type which redirects the preview to a new URL."""
        self.lookups["publish.twitter.com"] = [(IPv4Address, "10.1.2.3")]
        self.lookups["cdn.twitter.com"] = [(IPv4Address, "10.1.2.3")]

        result = {
            "version": "1.0",
            "type": "photo",
            "url": "http://cdn.twitter.com/matrixdotorg",
        }
        oembed_content = json.dumps(result).encode("utf-8")

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://twitter.com/matrixdotorg/status/12345",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: application/json; charset="utf8"\r\n\r\n'
            )
            % (len(oembed_content),)
            + oembed_content
        )

        self.pump()

        # Ensure a second request is made to the photo URL.
        client = self.reactor.tcpClients[1][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b"Content-Type: image/png\r\n\r\n"
            )
            % (len(SMALL_PNG),)
            + SMALL_PNG
        )

        self.pump()

        # Ensure the URL is what was requested.
        self.assertIn(b"/matrixdotorg", server.data)

        self.assertEqual(channel.code, 200)
        body = channel.json_body
        self.assertEqual(body["og:url"], "http://twitter.com/matrixdotorg/status/12345")
        self._assert_small_png(body)

    def test_oembed_rich(self) -> None:
        """Test an oEmbed endpoint which returns HTML content via the 'rich' type."""
        self.lookups["publish.twitter.com"] = [(IPv4Address, "10.1.2.3")]

        result = {
            "version": "1.0",
            "type": "rich",
            # Note that this provides the author, not the title.
            "author_name": "Alice",
            "html": "<div>Content Preview</div>",
        }
        end_content = json.dumps(result).encode("utf-8")

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://twitter.com/matrixdotorg/status/12345",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: application/json; charset="utf8"\r\n\r\n'
            )
            % (len(end_content),)
            + end_content
        )

        self.pump()

        # Double check that the proper host is being connected to. (Note that
        # twitter.com can't be resolved so this is already implicitly checked.)
        self.assertIn(b"\r\nHost: publish.twitter.com\r\n", server.data)

        self.assertEqual(channel.code, 200)
        body = channel.json_body
        self.assertEqual(
            body,
            {
                "og:url": "http://twitter.com/matrixdotorg/status/12345",
                "og:title": "Alice",
                "og:description": "Content Preview",
            },
        )

    def test_oembed_format(self) -> None:
        """Test an oEmbed endpoint which requires the format in the URL."""
        self.lookups["www.hulu.com"] = [(IPv4Address, "10.1.2.3")]

        result = {
            "version": "1.0",
            "type": "rich",
            "html": "<div>Content Preview</div>",
        }
        end_content = json.dumps(result).encode("utf-8")

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://www.hulu.com/watch/12345",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: application/json; charset="utf8"\r\n\r\n'
            )
            % (len(end_content),)
            + end_content
        )

        self.pump()

        # The {format} should have been turned into json.
        self.assertIn(b"/api/oembed.json", server.data)
        # A URL parameter of format=json should be provided.
        self.assertIn(b"format=json", server.data)

        self.assertEqual(channel.code, 200)
        body = channel.json_body
        self.assertEqual(
            body,
            {
                "og:url": "http://www.hulu.com/watch/12345",
                "og:description": "Content Preview",
            },
        )

    @unittest.override_config(
        {"url_preview_url_blacklist": [{"netloc": "publish.twitter.com"}]}
    )
    def test_oembed_blocked(self) -> None:
        """The oEmbed URL should not be downloaded if the oEmbed URL is blocked."""
        self.lookups["twitter.com"] = [(IPv4Address, "10.1.2.3")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://twitter.com/matrixdotorg/status/12345",
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(channel.code, 403, channel.result)

    def test_oembed_autodiscovery(self) -> None:
        """
        Autodiscovery works by finding the link in the HTML response and then requesting an oEmbed URL.
        1. Request a preview of a URL which is not known to the oEmbed code.
        2. It returns HTML including a link to an oEmbed preview.
        3. The oEmbed preview is requested and returns a URL for an image.
        4. The image is requested for thumbnailing.
        """
        # This is a little cheesy in that we use the www subdomain (which isn't the
        # list of oEmbed patterns) to get "raw" HTML response.
        self.lookups["www.twitter.com"] = [(IPv4Address, "10.1.2.3")]
        self.lookups["publish.twitter.com"] = [(IPv4Address, "10.1.2.3")]
        self.lookups["cdn.twitter.com"] = [(IPv4Address, "10.1.2.3")]

        result = b"""
        <link rel="alternate" type="application/json+oembed"
            href="http://publish.twitter.com/oembed?url=http%3A%2F%2Fcdn.twitter.com%2Fmatrixdotorg%2Fstatus%2F12345&format=json"
            title="matrixdotorg" />
        """

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://www.twitter.com/matrixdotorg/status/12345",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(result),)
            + result
        )
        self.pump()

        # The oEmbed response.
        result2 = {
            "version": "1.0",
            "type": "photo",
            "url": "http://cdn.twitter.com/matrixdotorg",
        }
        oembed_content = json.dumps(result2).encode("utf-8")

        # Ensure a second request is made to the oEmbed URL.
        client = self.reactor.tcpClients[1][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: application/json; charset="utf8"\r\n\r\n'
            )
            % (len(oembed_content),)
            + oembed_content
        )
        self.pump()

        # Ensure the URL is what was requested.
        self.assertIn(b"/oembed?", server.data)

        # Ensure a third request is made to the photo URL.
        client = self.reactor.tcpClients[2][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b"Content-Type: image/png\r\n\r\n"
            )
            % (len(SMALL_PNG),)
            + SMALL_PNG
        )
        self.pump()

        # Ensure the URL is what was requested.
        self.assertIn(b"/matrixdotorg", server.data)

        self.assertEqual(channel.code, 200)
        body = channel.json_body
        self.assertEqual(
            body["og:url"], "http://www.twitter.com/matrixdotorg/status/12345"
        )
        self._assert_small_png(body)

    @unittest.override_config(
        {"url_preview_url_blacklist": [{"netloc": "publish.twitter.com"}]}
    )
    def test_oembed_autodiscovery_blocked(self) -> None:
        """
        If the discovered oEmbed URL is blocked, it should be discarded.
        """
        # This is a little cheesy in that we use the www subdomain (which isn't the
        # list of oEmbed patterns) to get "raw" HTML response.
        self.lookups["www.twitter.com"] = [(IPv4Address, "10.1.2.3")]
        self.lookups["publish.twitter.com"] = [(IPv4Address, "10.1.2.4")]

        result = b"""
        <title>Test</title>
        <link rel="alternate" type="application/json+oembed"
            href="http://publish.twitter.com/oembed?url=http%3A%2F%2Fcdn.twitter.com%2Fmatrixdotorg%2Fstatus%2F12345&format=json"
            title="matrixdotorg" />
        """

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://www.twitter.com/matrixdotorg/status/12345",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            (
                b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n"
                b'Content-Type: text/html; charset="utf8"\r\n\r\n'
            )
            % (len(result),)
            + result
        )

        self.pump()

        # Ensure there's no additional connections.
        self.assertEqual(len(self.reactor.tcpClients), 1)

        # Ensure the URL is what was requested.
        self.assertIn(b"\r\nHost: www.twitter.com\r\n", server.data)

        self.assertEqual(channel.code, 200)
        body = channel.json_body
        self.assertEqual(body["og:title"], "Test")
        self.assertNotIn("og:image", body)

    def _download_image(self) -> tuple[str, str]:
        """Downloads an image into the URL cache.
        Returns:
            A (host, media_id) tuple representing the MXC URI of the image.
        """
        self.lookups["cdn.twitter.com"] = [(IPv4Address, "10.1.2.3")]

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=http://cdn.twitter.com/matrixdotorg",
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\nContent-Type: image/png\r\n\r\n"
            % (len(SMALL_PNG),)
            + SMALL_PNG
        )

        self.pump()
        self.assertEqual(channel.code, 200)
        body = channel.json_body
        mxc_uri = body["og:image"]
        host, _port, media_id = parse_and_validate_mxc_uri(mxc_uri)
        self.assertIsNone(_port)
        return host, media_id

    def test_storage_providers_exclude_files(self) -> None:
        """Test that files are not stored in or fetched from storage providers."""
        host, media_id = self._download_image()

        rel_file_path = self.media_repo.filepaths.url_cache_filepath_rel(media_id)
        media_store_path = os.path.join(self.media_store_path, rel_file_path)
        storage_provider_path = os.path.join(self.storage_path, rel_file_path)

        # Check storage
        self.assertTrue(os.path.isfile(media_store_path))
        self.assertFalse(
            os.path.isfile(storage_provider_path),
            "URL cache file was unexpectedly stored in a storage provider",
        )

        # Check fetching
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/download/{host}/{media_id}",
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(channel.code, 200)

        # Move cached file into the storage provider
        os.makedirs(os.path.dirname(storage_provider_path), exist_ok=True)
        os.rename(media_store_path, storage_provider_path)

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/download/{host}/{media_id}",
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(
            channel.code,
            404,
            "URL cache file was unexpectedly retrieved from a storage provider",
        )

    def test_storage_providers_exclude_thumbnails(self) -> None:
        """Test that thumbnails are not stored in or fetched from storage providers."""
        host, media_id = self._download_image()

        rel_thumbnail_path = (
            self.media_repo.filepaths.url_cache_thumbnail_directory_rel(media_id)
        )
        media_store_thumbnail_path = os.path.join(
            self.media_store_path, rel_thumbnail_path
        )
        storage_provider_thumbnail_path = os.path.join(
            self.storage_path, rel_thumbnail_path
        )

        # Check storage
        self.assertTrue(os.path.isdir(media_store_thumbnail_path))
        self.assertFalse(
            os.path.isdir(storage_provider_thumbnail_path),
            "URL cache thumbnails were unexpectedly stored in a storage provider",
        )

        # Check fetching
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/{host}/{media_id}?width=32&height=32&method=scale",
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(channel.code, 200)

        # Remove the original, otherwise thumbnails will regenerate
        rel_file_path = self.media_repo.filepaths.url_cache_filepath_rel(media_id)
        media_store_path = os.path.join(self.media_store_path, rel_file_path)
        os.remove(media_store_path)

        # Move cached thumbnails into the storage provider
        os.makedirs(os.path.dirname(storage_provider_thumbnail_path), exist_ok=True)
        os.rename(media_store_thumbnail_path, storage_provider_thumbnail_path)

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/{host}/{media_id}?width=32&height=32&method=scale",
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(
            channel.code,
            404,
            "URL cache thumbnail was unexpectedly retrieved from a storage provider",
        )

    def test_cache_expiry(self) -> None:
        """Test that URL cache files and thumbnails are cleaned up properly on expiry."""
        _host, media_id = self._download_image()

        file_path = self.media_repo.filepaths.url_cache_filepath(media_id)
        file_dirs = self.media_repo.filepaths.url_cache_filepath_dirs_to_delete(
            media_id
        )
        thumbnail_dir = self.media_repo.filepaths.url_cache_thumbnail_directory(
            media_id
        )
        thumbnail_dirs = self.media_repo.filepaths.url_cache_thumbnail_dirs_to_delete(
            media_id
        )

        self.assertTrue(os.path.isfile(file_path))
        self.assertTrue(os.path.isdir(thumbnail_dir))

        self.reactor.advance(IMAGE_CACHE_EXPIRY_MS * 1000 + 1)
        self.get_success(self.url_previewer._expire_url_cache_data())

        for path in [file_path] + file_dirs + [thumbnail_dir] + thumbnail_dirs:
            self.assertFalse(
                os.path.exists(path),
                f"{os.path.relpath(path, self.media_store_path)} was not deleted",
            )

    @unittest.override_config({"url_preview_url_blacklist": [{"port": "*"}]})
    def test_blocked_port(self) -> None:
        """Tests that blocking URLs with a port makes previewing such URLs
        fail with a 403 error and doesn't impact other previews.
        """
        self.lookups["matrix.org"] = [(IPv4Address, "10.1.2.3")]

        bad_url = quote("http://matrix.org:8888/foo")
        good_url = quote("http://matrix.org/foo")

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=" + bad_url,
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(channel.code, 403, channel.result)

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=" + good_url,
            shorthand=False,
            await_result=False,
        )
        self.pump()

        client = self.reactor.tcpClients[0][2].buildProtocol(None)
        server = AccumulatingProtocol()
        server.makeConnection(FakeTransport(client, self.reactor))
        client.makeConnection(FakeTransport(server, self.reactor))
        client.dataReceived(
            b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\nContent-Type: text/html\r\n\r\n"
            % (len(self.end_content),)
            + self.end_content
        )

        self.pump()
        self.assertEqual(channel.code, 200)

    @unittest.override_config(
        {"url_preview_url_blacklist": [{"netloc": "example.com"}]}
    )
    def test_blocked_url(self) -> None:
        """Tests that blocking URLs with a host makes previewing such URLs
        fail with a 403 error.
        """
        self.lookups["example.com"] = [(IPv4Address, "10.1.2.3")]

        bad_url = quote("http://example.com/foo")

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/preview_url?url=" + bad_url,
            shorthand=False,
            await_result=False,
        )
        self.pump()
        self.assertEqual(channel.code, 403, channel.result)


class MediaConfigTest(unittest.HomeserverTestCase):
    servlets = [
        media.register_servlets,
        admin.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(
        self, reactor: ThreadedMemoryReactorClock, clock: Clock
    ) -> HomeServer:
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
        self.register_user("user", "password")
        self.tok = self.login("user", "password")

    def test_media_config(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/config",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body["m.upload.size"], self.hs.config.media.max_upload_size
        )


class MediaConfigModuleCallbackTestCase(unittest.HomeserverTestCase):
    servlets = [
        media.register_servlets,
        admin.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(
        self, reactor: ThreadedMemoryReactorClock, clock: Clock
    ) -> HomeServer:
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
        self.user = self.register_user("user", "password")
        self.tok = self.login("user", "password")

        hs.get_module_api().register_media_repository_callbacks(
            get_media_config_for_user=self.get_media_config_for_user,
        )

    async def get_media_config_for_user(
        self,
        user_id: str,
    ) -> JsonDict | None:
        # We echo back the user_id and set a custom upload size.
        return {"m.upload.size": 1024, "user_id": user_id}

    def test_media_config(self) -> None:
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/config",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["m.upload.size"], 1024)
        self.assertEqual(channel.json_body["user_id"], self.user)


class RemoteDownloadLimiterTestCase(unittest.HomeserverTestCase):
    servlets = [
        media.register_servlets,
        login.register_servlets,
        admin.register_servlets,
    ]

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
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

    # mock actually reading file body
    def read_multipart_response_30MiB(*args: Any, **kwargs: Any) -> Deferred:
        d: Deferred = defer.Deferred()
        d.callback(MultipartResponse(b"{}", 31457280, b"img/png", None))
        return d

    def read_multipart_response_50MiB(*args: Any, **kwargs: Any) -> Deferred:
        d: Deferred = defer.Deferred()
        d.callback(MultipartResponse(b"{}", 31457280, b"img/png", None))
        return d

    @patch(
        "synapse.http.matrixfederationclient.read_multipart_response",
        read_multipart_response_30MiB,
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
            resp.headers = Headers(
                {"Content-Type": ["multipart/mixed; boundary=gc0p4Jq0M2Yt08jU534c0p"]}
            )
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # first request should go through
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abc",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 200

        # next 15 should go through
        for i in range(15):
            channel2 = self.make_request(
                "GET",
                f"/_matrix/client/v1/media/download/remote.org/abc{i}",
                shorthand=False,
                access_token=self.tok,
            )
            assert channel2.code == 200

        # 17th will hit ratelimit
        channel3 = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcd",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel3.code == 429

        # however, a request from a different IP will go through
        channel4 = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcde",
            shorthand=False,
            client_ip="187.233.230.159",
            access_token=self.tok,
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
            "/_matrix/client/v1/media/download/remote.org/abcdef",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel5.code == 200

    @override_config(
        {
            "remote_media_download_per_second": "50M",
            "remote_media_download_burst_count": "50M",
        }
    )
    @patch(
        "synapse.http.matrixfederationclient.read_multipart_response",
        read_multipart_response_50MiB,
    )
    def test_download_rate_limit_config(self) -> None:
        """
        Test that download rate limit config options are correctly picked up and applied
        """

        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = 52428800
            resp.headers = Headers(
                {"Content-Type": ["multipart/mixed; boundary=gc0p4Jq0M2Yt08jU534c0p"]}
            )
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # first request should go through
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abc",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 200

        # immediate second request should fail
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcd",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 429

        # advance half a second
        self.reactor.pump([0.5])

        # request still fails
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcde",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 429

        # advance another half second
        self.reactor.pump([0.5])

        # enough has drained from bucket and request is successful
        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcdef",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 200

    @override_config(
        {
            "remote_media_download_burst_count": "87M",
        }
    )
    @patch(
        "synapse.http.matrixfederationclient.read_multipart_response",
        read_multipart_response_30MiB,
    )
    def test_download_ratelimit_unknown_length(self) -> None:
        """
        Test that if no content-length is provided, ratelimiting is still applied after
        media is downloaded and length is known
        """

        # mock out actually sending the request
        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = UNKNOWN_LENGTH
            resp.headers = Headers(
                {"Content-Type": ["multipart/mixed; boundary=gc0p4Jq0M2Yt08jU534c0p"]}
            )
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        # first 3 will go through (note that 3rd request technically violates rate limit but
        # that since the ratelimiting is applied *after* download it goes through, but next one fails)
        for i in range(3):
            channel2 = self.make_request(
                "GET",
                f"/_matrix/client/v1/media/download/remote.org/abc{i}",
                shorthand=False,
                access_token=self.tok,
            )
            assert channel2.code == 200

        # 4th will hit ratelimit
        channel3 = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcd",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel3.code == 429

    @override_config({"max_upload_size": "29M"})
    @patch(
        "synapse.http.matrixfederationclient.read_multipart_response",
        read_multipart_response_30MiB,
    )
    def test_max_download_respected(self) -> None:
        """
        Test that the max download size is enforced - note that max download size is determined
        by the max_upload_size
        """

        # mock out actually sending the request, returns a 30MiB response
        async def _send_request(*args: Any, **kwargs: Any) -> IResponse:
            resp = MagicMock(spec=IResponse)
            resp.code = 200
            resp.length = 31457280
            resp.headers = Headers(
                {"Content-Type": ["multipart/mixed; boundary=gc0p4Jq0M2Yt08jU534c0p"]}
            )
            resp.phrase = b"OK"
            return resp

        self.client._send_request = _send_request  # type: ignore

        channel = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/remote.org/abcd",
            shorthand=False,
            access_token=self.tok,
        )
        assert channel.code == 502
        assert channel.json_body["errcode"] == "M_TOO_LARGE"

    def test_file_download(self) -> None:
        content = io.BytesIO(b"file_to_stream")
        content_uri = self.get_success(
            self.repo.create_or_update_content(
                "text/plain",
                "test_upload",
                content,
                46,
                UserID.from_string("@user_id:whatever.org"),
            )
        )
        # test with a text file
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/download/test/{content_uri.media_id}",
            shorthand=False,
            access_token=self.tok,
        )
        self.pump()
        self.assertEqual(200, channel.code)


test_images = [
    small_png,
    small_png_with_transparency,
    small_cmyk_jpeg,
    small_lossless_webp,
    empty_file,
    SVG,
]
input_values = [(x,) for x in test_images]


@parameterized_class(("test_image",), input_values)
class DownloadAndThumbnailTestCase(unittest.HomeserverTestCase):
    test_image: ClassVar[TestImage]
    servlets = [
        media.register_servlets,
        login.register_servlets,
        admin.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.fetches: list[
            tuple[
                "Deferred[Any]",
                str,
                str,
                QueryParams | None,
            ]
        ] = []

        def federation_get_file(
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
        ) -> "Deferred[tuple[int, dict[bytes, list[bytes]], bytes]]":
            """A mock for MatrixFederationHttpClient.federation_get_file."""

            def write_to(
                r: tuple[bytes, tuple[int, dict[bytes, list[bytes]], bytes]],
            ) -> tuple[int, dict[bytes, list[bytes]], bytes]:
                data, response = r
                output_stream.write(data)
                return response

            def write_err(f: Failure) -> Failure:
                f.trap(HttpResponseException)
                output_stream.write(f.value.response)
                return f

            d: Deferred[tuple[bytes, tuple[int, dict[bytes, list[bytes]], bytes]]] = (
                Deferred()
            )
            self.fetches.append((d, destination, path, args))
            # Note that this callback changes the value held by d.
            d_after_callback = d.addCallbacks(write_to, write_err)
            return make_deferred_yieldable(d_after_callback)

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
        client.federation_get_file = federation_get_file
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

        self.remote = "example.com"
        self.media_id = "12345"

        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

    def _req(
        self, content_disposition: bytes | None, include_content_type: bool = True
    ) -> FakeChannel:
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/download/{self.remote}/{self.media_id}",
            shorthand=False,
            await_result=False,
            access_token=self.tok,
        )
        self.pump()

        # We've made one fetch, to example.com, using the federation media URL
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.fetches[0][1], "example.com")
        self.assertEqual(
            self.fetches[0][2], "/_matrix/federation/v1/media/download/" + self.media_id
        )
        self.assertEqual(
            self.fetches[0][3],
            {"timeout_ms": "20000"},
        )

        headers = {
            b"Content-Length": [b"%d" % (len(self.test_image.data))],
        }

        if include_content_type:
            headers[b"Content-Type"] = [self.test_image.content_type]

        if content_disposition:
            headers[b"Content-Disposition"] = [content_disposition]

        self.fetches[0][0].callback(
            (self.test_image.data, (len(self.test_image.data), headers, b"{}"))
        )

        self.pump()
        self.assertEqual(channel.code, 200)

        return channel

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

    def test_unknown_federation_endpoint(self) -> None:
        """
        Test that if the download request to remote federation endpoint returns a 404
        we fall back to the _matrix/media endpoint
        """
        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/download/{self.remote}/{self.media_id}",
            shorthand=False,
            await_result=False,
            access_token=self.tok,
        )
        self.pump()

        # We've made one fetch, to example.com, using the media URL, and asking
        # the other server not to do a remote fetch
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.fetches[0][1], "example.com")
        self.assertEqual(
            self.fetches[0][2], f"/_matrix/federation/v1/media/download/{self.media_id}"
        )

        # The result which says the endpoint is unknown.
        unknown_endpoint = b'{"errcode":"M_UNRECOGNIZED","error":"Unknown request"}'
        self.fetches[0][0].errback(
            HttpResponseException(404, "NOT FOUND", unknown_endpoint)
        )

        self.pump()

        # There should now be another request to the _matrix/media/v3/download URL.
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.fetches[1][1], "example.com")
        self.assertEqual(
            self.fetches[1][2],
            f"/_matrix/media/v3/download/example.com/{self.media_id}",
        )

        headers = {
            b"Content-Length": [b"%d" % (len(self.test_image.data))],
        }

        self.fetches[1][0].callback(
            (self.test_image.data, (len(self.test_image.data), headers))
        )

        self.pump()
        self.assertEqual(channel.code, 200)

    def test_thumbnail_crop(self) -> None:
        """Test that a cropped remote thumbnail is available."""
        self._test_thumbnail(
            "crop",
            self.test_image.expected_cropped,
            expected_found=self.test_image.expected_found,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
        )

    def test_thumbnail_scale(self) -> None:
        """Test that a scaled remote thumbnail is available."""
        self._test_thumbnail(
            "scale",
            self.test_image.expected_scaled,
            expected_found=self.test_image.expected_found,
            unable_to_thumbnail=self.test_image.unable_to_thumbnail,
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
        {"thumbnail_sizes": [{"width": 32, "height": 32, "method": "scale"}]}
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
        {"thumbnail_sizes": [{"width": 32, "height": 32, "method": "crop"}]}
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
            f"/_matrix/client/v1/media/thumbnail/{self.remote}/{self.media_id}{params}",
            shorthand=False,
            await_result=False,
            access_token=self.tok,
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
        info = self.get_success(
            self.store.get_cached_remote_media(self.remote, self.media_id)
        )
        assert info is not None
        file_id = info.filesystem_id

        thumbnail_dir = self.media_repo.filepaths.remote_media_thumbnail_dir(
            self.remote, file_id
        )
        shutil.rmtree(thumbnail_dir, ignore_errors=True)

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/{self.remote}/{self.media_id}{params}",
            shorthand=False,
            await_result=False,
            access_token=self.tok,
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
            f"/_matrix/client/v1/media/thumbnail/{self.remote}/{self.media_id}{params}",
            shorthand=False,
            await_result=False,
            access_token=self.tok,
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
                    channel.result["body"], expected_body, channel.result["body"].hex()
                )
            else:
                # ensure that the result is at least some valid image
                Image.open(io.BytesIO(channel.result["body"]))
        elif unable_to_thumbnail:
            # A 400 with a JSON body.
            self.assertEqual(channel.code, 400)
            self.assertEqual(
                channel.json_body,
                {
                    "errcode": "M_UNKNOWN",
                    "error": "Cannot find any thumbnails for the requested media ('/_matrix/client/v1/media/thumbnail/example.com/12345'). This might mean the media is not a supported_media_format=(image/jpeg, image/jpg, image/webp, image/gif, image/png) or that thumbnailing failed for some other reason. (Dynamic thumbnails are disabled on this server.)",
                },
            )
        else:
            # A 404 with a JSON body.
            self.assertEqual(channel.code, 404)
            self.assertEqual(
                channel.json_body,
                {
                    "errcode": "M_NOT_FOUND",
                    "error": "Not found '/_matrix/client/v1/media/thumbnail/example.com/12345'",
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


configs = [
    {"extra_config": {"dynamic_thumbnails": True}},
    {"extra_config": {"dynamic_thumbnails": False}},
]


@parameterized_class(configs)
class AuthenticatedMediaTestCase(unittest.HomeserverTestCase):
    extra_config: dict[str, Any]
    servlets = [
        media.register_servlets,
        login.register_servlets,
        admin.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()

        self.clock = clock
        self.storage_path = self.mktemp()
        self.media_store_path = self.mktemp()
        os.mkdir(self.storage_path)
        os.mkdir(self.media_store_path)
        config["media_store_path"] = self.media_store_path
        config["enable_authenticated_media"] = True

        provider_config = {
            "module": "synapse.media.storage_provider.FileStorageProviderBackend",
            "store_local": True,
            "store_synchronous": False,
            "store_remote": True,
            "config": {"directory": self.storage_path},
        }

        config["media_storage_providers"] = [provider_config]
        config.update(self.extra_config)

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.repo = hs.get_media_repository()
        self.client = hs.get_federation_http_client()
        self.store = hs.get_datastores().main
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def test_authenticated_media(self) -> None:
        # upload some local media with authentication on
        channel = self.make_request(
            "POST",
            "_matrix/media/v3/upload?filename=test_png_upload",
            SMALL_PNG,
            self.tok,
            shorthand=False,
            content_type=b"image/png",
            custom_headers=[("Content-Length", str(67))],
        )
        self.assertEqual(channel.code, 200)
        res = channel.json_body.get("content_uri")
        assert res is not None
        uri = res.split("mxc://")[1]

        # request media over authenticated endpoint, should be found
        channel2 = self.make_request(
            "GET",
            f"_matrix/client/v1/media/download/{uri}",
            access_token=self.tok,
            shorthand=False,
        )
        self.assertEqual(channel2.code, 200)

        # request same media over unauthenticated media, should raise 404 not found
        channel3 = self.make_request(
            "GET", f"_matrix/media/v3/download/{uri}", shorthand=False
        )
        self.assertEqual(channel3.code, 404)

        # check thumbnails as well
        params = "?width=32&height=32&method=crop"
        channel4 = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/{uri}{params}",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel4.code, 200)

        params = "?width=32&height=32&method=crop"
        channel5 = self.make_request(
            "GET",
            f"/_matrix/media/r0/thumbnail/{uri}{params}",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel5.code, 404)

        # Inject a piece of remote media.
        file_id = "abcdefg12345"
        file_info = FileInfo(server_name="lonelyIsland", file_id=file_id)

        media_storage = self.hs.get_media_repository().media_storage

        ctx = media_storage.store_into_file(file_info)
        (f, fname) = self.get_success(ctx.__aenter__())
        f.write(SMALL_PNG)
        self.get_success(ctx.__aexit__(None, None, None))

        # we write the authenticated status when storing media, so this should pick up
        # config and authenticate the media
        self.get_success(
            self.store.store_cached_remote_media(
                origin="lonelyIsland",
                media_id="52",
                media_type="image/png",
                media_length=1,
                time_now_ms=self.clock.time_msec(),
                upload_name="remote_test.png",
                filesystem_id=file_id,
                sha256=file_id,
            )
        )

        # ensure we have thumbnails for the non-dynamic code path
        if self.extra_config == {"dynamic_thumbnails": False}:
            self.get_success(
                self.repo._generate_thumbnails(
                    "lonelyIsland", "52", file_id, "image/png"
                )
            )

        channel6 = self.make_request(
            "GET",
            "_matrix/client/v1/media/download/lonelyIsland/52",
            access_token=self.tok,
            shorthand=False,
        )
        self.assertEqual(channel6.code, 200)

        channel7 = self.make_request(
            "GET", f"_matrix/media/v3/download/{uri}", shorthand=False
        )
        self.assertEqual(channel7.code, 404)

        params = "?width=32&height=32&method=crop"
        channel8 = self.make_request(
            "GET",
            f"/_matrix/client/v1/media/thumbnail/lonelyIsland/52{params}",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel8.code, 200)

        channel9 = self.make_request(
            "GET",
            f"/_matrix/media/r0/thumbnail/lonelyIsland/52{params}",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel9.code, 404)

        # Inject a piece of local media that isn't authenticated
        file_id = "abcdefg123456"
        file_info = FileInfo(None, file_id=file_id)

        ctx = media_storage.store_into_file(file_info)
        (f, fname) = self.get_success(ctx.__aenter__())
        f.write(SMALL_PNG)
        self.get_success(ctx.__aexit__(None, None, None))

        self.get_success(
            self.store.db_pool.simple_insert(
                "local_media_repository",
                {
                    "media_id": "abcdefg123456",
                    "media_type": "image/png",
                    "created_ts": self.clock.time_msec(),
                    "upload_name": "test_local",
                    "media_length": 1,
                    "user_id": "someone",
                    "url_cache": None,
                    "authenticated": False,
                },
                desc="store_local_media",
            )
        )

        # check that unauthenticated media is still available over both endpoints
        channel9 = self.make_request(
            "GET",
            "/_matrix/client/v1/media/download/test/abcdefg123456",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel9.code, 200)

        channel10 = self.make_request(
            "GET",
            "/_matrix/media/r0/download/test/abcdefg123456",
            shorthand=False,
            access_token=self.tok,
        )
        self.assertEqual(channel10.code, 200)

    def test_authenticated_media_etag(self) -> None:
        """Test that ETag works correctly with authenticated media over client
        APIs"""

        # upload some local media with authentication on
        channel = self.make_request(
            "POST",
            "_matrix/media/v3/upload?filename=test_png_upload",
            SMALL_PNG,
            self.tok,
            shorthand=False,
            content_type=b"image/png",
            custom_headers=[("Content-Length", str(67))],
        )
        self.assertEqual(channel.code, 200)
        res = channel.json_body.get("content_uri")
        assert res is not None
        uri = res.split("mxc://")[1]

        # Check standard media endpoint
        self._check_caching(f"/download/{uri}")

        # check thumbnails as well
        params = "?width=32&height=32&method=crop"
        self._check_caching(f"/thumbnail/{uri}{params}")

        # Inject a piece of remote media.
        file_id = "abcdefg12345"
        file_info = FileInfo(server_name="lonelyIsland", file_id=file_id)

        media_storage = self.hs.get_media_repository().media_storage

        ctx = media_storage.store_into_file(file_info)
        (f, fname) = self.get_success(ctx.__aenter__())
        f.write(SMALL_PNG)
        self.get_success(ctx.__aexit__(None, None, None))

        # we write the authenticated status when storing media, so this should pick up
        # config and authenticate the media
        self.get_success(
            self.store.store_cached_remote_media(
                origin="lonelyIsland",
                media_id="52",
                media_type="image/png",
                media_length=1,
                time_now_ms=self.clock.time_msec(),
                upload_name="remote_test.png",
                filesystem_id=file_id,
                sha256=file_id,
            )
        )

        # ensure we have thumbnails for the non-dynamic code path
        if self.extra_config == {"dynamic_thumbnails": False}:
            self.get_success(
                self.repo._generate_thumbnails(
                    "lonelyIsland", "52", file_id, "image/png"
                )
            )

        self._check_caching("/download/lonelyIsland/52")

        params = "?width=32&height=32&method=crop"
        self._check_caching(f"/thumbnail/lonelyIsland/52{params}")

    def _check_caching(self, path: str) -> None:
        """
        Checks that:
          1. fetching the path returns an ETag header
          2. refetching with the ETag returns a 304 without a body
          3. refetching with the ETag but through unauthenticated endpoint
             returns 404
        """

        # Request media over authenticated endpoint, should be found
        channel1 = self.make_request(
            "GET",
            f"/_matrix/client/v1/media{path}",
            access_token=self.tok,
            shorthand=False,
        )
        self.assertEqual(channel1.code, 200)

        # Should have a single ETag field
        etags = channel1.headers.getRawHeaders("ETag")
        self.assertIsNotNone(etags)
        assert etags is not None  # For mypy
        self.assertEqual(len(etags), 1)
        etag = etags[0]

        # Refetching with the etag should result in 304 and empty body.
        channel2 = self.make_request(
            "GET",
            f"/_matrix/client/v1/media{path}",
            access_token=self.tok,
            shorthand=False,
            custom_headers=[("If-None-Match", etag)],
        )
        self.assertEqual(channel2.code, 304)
        self.assertEqual(channel2.is_finished(), True)
        self.assertNotIn("body", channel2.result)

        # Refetching with the etag but no access token should result in 404.
        channel3 = self.make_request(
            "GET",
            f"/_matrix/media/r0{path}",
            shorthand=False,
            custom_headers=[("If-None-Match", etag)],
        )
        self.assertEqual(channel3.code, 404)


class MediaUploadLimits(unittest.HomeserverTestCase):
    """
    This test case simulates a homeserver with media upload limits configured.
    """

    servlets = [
        media.register_servlets,
        login.register_servlets,
        admin.register_servlets,
    ]

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

        # These are the limits that we are testing
        config["media_upload_limits"] = [
            {"time_period": "1d", "max_size": "1K"},
            {"time_period": "1w", "max_size": "3K"},
        ]

        return self.setup_test_homeserver(config=config)

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.repo = hs.get_media_repository()
        self.client = hs.get_federation_http_client()
        self.store = hs.get_datastores().main
        self.user = self.register_user("user", "pass")
        self.tok = self.login("user", "pass")

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def upload_media(self, size: int) -> FakeChannel:
        """Helper to upload media of a given size."""
        return self.make_request(
            "POST",
            "/_matrix/media/v3/upload",
            content=b"0" * size,
            access_token=self.tok,
            shorthand=False,
            content_type=b"text/plain",
            custom_headers=[("Content-Length", str(size))],
        )

    def test_upload_under_limit(self) -> None:
        """Test that uploading media under the limit works."""
        channel = self.upload_media(67)
        self.assertEqual(channel.code, 200)

    def test_over_day_limit(self) -> None:
        """Test that uploading media over the daily limit fails."""
        channel = self.upload_media(500)
        self.assertEqual(channel.code, 200)

        channel = self.upload_media(800)
        self.assertEqual(channel.code, 400)

    def test_under_daily_limit(self) -> None:
        """Test that uploading media under the daily limit fails."""
        channel = self.upload_media(500)
        self.assertEqual(channel.code, 200)

        self.reactor.advance(60 * 60 * 24)  # Advance by one day

        # This will succeed as the daily limit has reset
        channel = self.upload_media(800)
        self.assertEqual(channel.code, 200)

        self.reactor.advance(60 * 60 * 24)  # Advance by one day

        # ... and again
        channel = self.upload_media(800)
        self.assertEqual(channel.code, 200)

    def test_over_weekly_limit(self) -> None:
        """Test that uploading media over the weekly limit fails."""
        channel = self.upload_media(900)
        self.assertEqual(channel.code, 200)

        self.reactor.advance(60 * 60 * 24)  # Advance by one day

        channel = self.upload_media(900)
        self.assertEqual(channel.code, 200)

        self.reactor.advance(2 * 60 * 60 * 24)  # Advance by one day

        channel = self.upload_media(900)
        self.assertEqual(channel.code, 200)

        self.reactor.advance(2 * 60 * 60 * 24)  # Advance by one day

        # This will fail as the weekly limit has been exceeded
        channel = self.upload_media(900)
        self.assertEqual(channel.code, 400)

        # Reset the weekly limit by advancing a week
        self.reactor.advance(7 * 60 * 60 * 24)  # Advance by 7 days

        # This will succeed as the weekly limit has reset
        channel = self.upload_media(900)
        self.assertEqual(channel.code, 200)


class MediaUploadLimitsModuleOverrides(unittest.HomeserverTestCase):
    """
    This test case simulates a homeserver with media upload limits being overridden by the module API.
    """

    servlets = [
        media.register_servlets,
        login.register_servlets,
        admin.register_servlets,
    ]

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

        # default limits to use
        config["media_upload_limits"] = [
            {"time_period": "1d", "max_size": "1K"},
            {"time_period": "1w", "max_size": "3K"},
        ]

        return self.setup_test_homeserver(config=config)

    async def _get_media_upload_limits_for_user(
        self,
        user_id: str,
    ) -> list[MediaUploadLimit] | None:
        # user1 has custom limits
        if user_id == self.user1:
            # n.b. we return these in increasing duration order and Synapse will need to sort them correctly
            return [
                MediaUploadLimit(
                    time_period_ms=Config.parse_duration("1d"), max_bytes=5000
                ),
                MediaUploadLimit(
                    time_period_ms=Config.parse_duration("1w"), max_bytes=15000
                ),
            ]
        # user2 has no limits
        if user_id == self.user2:
            return []
        # otherwise use default
        return None

    async def _on_media_upload_limit_exceeded(
        self,
        user_id: str,
        limit: MediaUploadLimit,
        sent_bytes: int,
        attempted_bytes: int,
    ) -> None:
        self.last_media_upload_limit_exceeded: dict[str, object] | None = {
            "user_id": user_id,
            "limit": limit,
            "sent_bytes": sent_bytes,
            "attempted_bytes": attempted_bytes,
        }

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.repo = hs.get_media_repository()
        self.client = hs.get_federation_http_client()
        self.store = hs.get_datastores().main
        self.user1 = self.register_user("user1", "pass")
        self.tok1 = self.login("user1", "pass")
        self.user2 = self.register_user("user2", "pass")
        self.tok2 = self.login("user2", "pass")
        self.user3 = self.register_user("user3", "pass")
        self.tok3 = self.login("user3", "pass")
        self.last_media_upload_limit_exceeded = None
        self.hs.get_module_api().register_media_repository_callbacks(
            get_media_upload_limits_for_user=self._get_media_upload_limits_for_user,
            on_media_upload_limit_exceeded=self._on_media_upload_limit_exceeded,
        )

    def create_resource_dict(self) -> dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def upload_media(self, size: int, tok: str) -> FakeChannel:
        """Helper to upload media of a given size with a given token."""
        return self.make_request(
            "POST",
            "/_matrix/media/v3/upload",
            content=b"0" * size,
            access_token=tok,
            shorthand=False,
            content_type=b"text/plain",
            custom_headers=[("Content-Length", str(size))],
        )

    def test_upload_under_limit(self) -> None:
        """Test that uploading media under the limit works."""

        # User 1 uploads 100 bytes
        channel = self.upload_media(100, self.tok1)
        self.assertEqual(channel.code, 200)

        # User 2 (unlimited) uploads 100 bytes
        channel = self.upload_media(100, self.tok2)
        self.assertEqual(channel.code, 200)

        # User 3 (default) uploads 100 bytes
        channel = self.upload_media(100, self.tok3)
        self.assertEqual(channel.code, 200)

        self.assertEqual(self.last_media_upload_limit_exceeded, None)

    def test_uses_custom_limit(self) -> None:
        """Test that uploading media over the module provided daily limit fails."""

        # User 1 uploads 3000 bytes
        channel = self.upload_media(3000, self.tok1)
        self.assertEqual(channel.code, 200)

        # User 1 attempts to upload 4000 bytes taking it over the limit
        channel = self.upload_media(4000, self.tok1)
        self.assertEqual(channel.code, 400)
        assert self.last_media_upload_limit_exceeded is not None
        self.assertEqual(self.last_media_upload_limit_exceeded["user_id"], self.user1)
        self.assertEqual(
            self.last_media_upload_limit_exceeded["limit"],
            MediaUploadLimit(
                max_bytes=5000, time_period_ms=Config.parse_duration("1d")
            ),
        )
        self.assertEqual(self.last_media_upload_limit_exceeded["sent_bytes"], 3000)
        self.assertEqual(self.last_media_upload_limit_exceeded["attempted_bytes"], 4000)

        # User 1 attempts to upload 20000 bytes which is over the weekly limit
        # This tests that the limits have been sorted as expected
        channel = self.upload_media(20000, self.tok1)
        self.assertEqual(channel.code, 400)
        assert self.last_media_upload_limit_exceeded is not None
        self.assertEqual(self.last_media_upload_limit_exceeded["user_id"], self.user1)
        self.assertEqual(
            self.last_media_upload_limit_exceeded["limit"],
            MediaUploadLimit(
                max_bytes=15000, time_period_ms=Config.parse_duration("1w")
            ),
        )
        self.assertEqual(self.last_media_upload_limit_exceeded["sent_bytes"], 3000)
        self.assertEqual(
            self.last_media_upload_limit_exceeded["attempted_bytes"], 20000
        )

    def test_uses_unlimited(self) -> None:
        """Test that unlimited user is not limited when module returns []."""
        # User 2 uploads 10000 bytes which is over the default limit
        channel = self.upload_media(10000, self.tok2)
        self.assertEqual(channel.code, 200)
        self.assertEqual(self.last_media_upload_limit_exceeded, None)

    def test_uses_defaults(self) -> None:
        """Test that the default limits are applied when module returned None."""
        # User 3 uploads 500 bytes
        channel = self.upload_media(500, self.tok3)
        self.assertEqual(channel.code, 200)

        # User 3 uploads 800 bytes which is over the limit
        channel = self.upload_media(800, self.tok3)
        self.assertEqual(channel.code, 400)
        assert self.last_media_upload_limit_exceeded is not None
        self.assertEqual(self.last_media_upload_limit_exceeded["user_id"], self.user3)
        self.assertEqual(
            self.last_media_upload_limit_exceeded["limit"],
            MediaUploadLimit(
                max_bytes=1024, time_period_ms=Config.parse_duration("1d")
            ),
        )
        self.assertEqual(self.last_media_upload_limit_exceeded["sent_bytes"], 500)
        self.assertEqual(self.last_media_upload_limit_exceeded["attempted_bytes"], 800)
