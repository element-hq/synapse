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
from io import BytesIO
from typing import Any, Dict, Literal, Optional, Tuple, Union
from unittest.mock import MagicMock, patch

from twisted.internet import defer
from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor
from twisted.web.http_headers import Headers
from twisted.web.iweb import IResponse
from twisted.web.resource import Resource

from synapse.api.errors import Codes
from synapse.events import EventBase
from synapse.http.client import ByteWriteable, MultipartResponse
from synapse.media._base import FileInfo
from synapse.media.filepath import MediaFilePaths
from synapse.media.media_storage import MediaStorage, ReadableFileWrapper
from synapse.media.storage_provider import FileStorageProviderBackend
from synapse.module_api import ModuleApi
from synapse.module_api.callbacks.spamchecker_callbacks import load_legacy_spam_checkers
from synapse.rest import admin
from synapse.rest.client import login, media
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomAlias
from synapse.util import Clock

from tests import unittest
from tests.test_utils import SMALL_PNG, SMALL_PNG_SHA256
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


class TestSpamCheckerLegacy:
    """A spam checker module that rejects all media that includes the bytes
    `evil`.

    Uses the legacy Spam-Checker API.
    """

    def __init__(self, config: Dict[str, Any], api: ModuleApi) -> None:
        self.config = config
        self.api = api

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> Dict[str, Any]:
        return config

    async def check_event_for_spam(self, event: EventBase) -> Union[bool, str]:
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

    def create_resource_dict(self) -> Dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    def default_config(self) -> Dict[str, Any]:
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

    def create_resource_dict(self) -> Dict[str, Resource]:
        resources = super().create_resource_dict()
        resources["/_matrix/media"] = self.hs.get_media_repository_resource()
        return resources

    async def check_media_file_for_spam(
        self, file_wrapper: ReadableFileWrapper, file_info: FileInfo
    ) -> Union[Codes, Literal["NOT_SPAM"], Tuple[Codes, JsonDict]]:
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


def read_multipart_response(
    response: IResponse, stream: ByteWriteable, boundary: str, max_length: Optional[int]
) -> Deferred:
    d: Deferred = defer.Deferred()
    stream.write(SMALL_PNG)
    d.callback(MultipartResponse(b"{}", 31457280, b"img/png", None))
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

    def create_resource_dict(self) -> Dict[str, Resource]:
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

    # mock actually reading file body
    @patch(
        "synapse.http.matrixfederationclient.read_multipart_response",
        read_multipart_response,
    )
    def test_ensure_correct_sha256_federated(self) -> None:
        """Check that federated media have the same hash."""

        # Mock getting a file over federation
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

    def create_resource_dict(self) -> Dict[str, Resource]:
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
