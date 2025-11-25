#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
from http import HTTPStatus
from typing import BinaryIO, Callable
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor
from twisted.web.http_headers import Headers

from synapse.api.errors import Codes, SynapseError
from synapse.http.client import RawHeaders
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils import SMALL_PNG, FakeResponse


class TestSSOHandler(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.http_client = Mock(spec=["get_file"])
        self.http_client.get_file.side_effect = mock_get_file
        self.http_client.user_agent = b"Synapse Test"
        hs = self.setup_test_homeserver(
            proxied_blocklisted_http_client=self.http_client
        )
        return hs

    def test_set_avatar(self) -> None:
        """Tests successfully setting the avatar of a newly created user"""
        handler = self.hs.get_sso_handler()

        # Create a new user to set avatar for
        reg_handler = self.hs.get_registration_handler()
        user_id = self.get_success(reg_handler.register_user(approved=True))

        self.assertTrue(
            self.get_success(handler.set_avatar(user_id, "http://my.server/me.png"))
        )

        # Ensure avatar is set on this newly created user,
        # so no need to compare for the exact image
        profile_handler = self.hs.get_profile_handler()
        profile = self.get_success(profile_handler.get_profile(user_id))
        self.assertIsNot(profile["avatar_url"], None)

    @unittest.override_config({"max_avatar_size": 1})
    def test_set_avatar_too_big_image(self) -> None:
        """Tests that saving an avatar fails when it is too big"""
        handler = self.hs.get_sso_handler()

        # any random user works since image check is supposed to fail
        user_id = "@sso-user:test"

        self.assertFalse(
            self.get_success(handler.set_avatar(user_id, "http://my.server/me.png"))
        )

    @unittest.override_config({"allowed_avatar_mimetypes": ["image/jpeg"]})
    def test_set_avatar_incorrect_mime_type(self) -> None:
        """Tests that saving an avatar fails when its mime type is not allowed"""
        handler = self.hs.get_sso_handler()

        # any random user works since image check is supposed to fail
        user_id = "@sso-user:test"

        self.assertFalse(
            self.get_success(handler.set_avatar(user_id, "http://my.server/me.png"))
        )

    def test_skip_saving_avatar_when_not_changed(self) -> None:
        """Tests whether saving of avatar correctly skips if the avatar hasn't
        changed"""
        handler = self.hs.get_sso_handler()

        # Create a new user to set avatar for
        reg_handler = self.hs.get_registration_handler()
        user_id = self.get_success(reg_handler.register_user(approved=True))

        # set avatar for the first time, should be a success
        self.assertTrue(
            self.get_success(handler.set_avatar(user_id, "http://my.server/me.png"))
        )

        # get avatar picture for comparison after another attempt
        profile_handler = self.hs.get_profile_handler()
        profile = self.get_success(profile_handler.get_profile(user_id))
        url_to_match = profile["avatar_url"]

        # set same avatar for the second time, should be a success
        self.assertTrue(
            self.get_success(handler.set_avatar(user_id, "http://my.server/me.png"))
        )

        # compare avatar picture's url from previous step
        profile = self.get_success(profile_handler.get_profile(user_id))
        self.assertEqual(profile["avatar_url"], url_to_match)


async def mock_get_file(
    url: str,
    output_stream: BinaryIO,
    max_size: int | None = None,
    headers: RawHeaders | None = None,
    is_allowed_content_type: Callable[[str], bool] | None = None,
) -> tuple[int, dict[bytes, list[bytes]], str, int]:
    fake_response = FakeResponse(code=404)
    if url == "http://my.server/me.png":
        fake_response = FakeResponse(
            code=200,
            headers=Headers(
                {"Content-Type": ["image/png"], "Content-Length": [str(len(SMALL_PNG))]}
            ),
            body=SMALL_PNG,
        )

    if max_size is not None and max_size < len(SMALL_PNG):
        raise SynapseError(
            HTTPStatus.BAD_GATEWAY,
            "Requested file is too large > %r bytes" % (max_size,),
            Codes.TOO_LARGE,
        )

    if is_allowed_content_type and not is_allowed_content_type("image/png"):
        raise SynapseError(
            HTTPStatus.BAD_GATEWAY,
            (
                "Requested file's content type not allowed for this operation: %s"
                % "image/png"
            ),
        )

    output_stream.write(fake_response.body)

    return len(SMALL_PNG), {b"Content-Type": [b"image/png"]}, "", 200
