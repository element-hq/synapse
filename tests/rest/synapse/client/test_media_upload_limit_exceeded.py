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


from twisted.web.resource import Resource

from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.rest.synapse.client.media_upload_limit_exceeded import (
    MEDIA_UPLOAD_LIMIT_EXCEEDED_PATH,
)

from tests import unittest


class MediaUploadLimitExceededPageTests(unittest.HomeserverTestCase):
    """Tests for the fallback page used as the `info_uri` for media upload
    limits that don't specify one."""

    def create_resource_dict(self) -> dict[str, Resource]:
        base = super().create_resource_dict()
        base.update(build_synapse_client_resource_tree(self.hs))
        return base

    def _assert_page_served(self) -> None:
        channel = self.make_request(
            "GET",
            MEDIA_UPLOAD_LIMIT_EXCEEDED_PATH,
            shorthand=False,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.headers.getRawHeaders("Content-Type"),
            ["text/html; charset=utf-8"],
        )
        self.assertIn(b"upload limit", channel.result["body"])

    def test_page_served(self) -> None:
        "The fallback page is served on a process running the media repo."
        self._assert_page_served()

    @unittest.override_config({"enable_media_repo": False})
    def test_page_served_without_media_repo(self) -> None:
        """The fallback page is served even on processes which don't run the
        media repo: in a split-worker deployment the media worker generates the
        `M_USER_LIMIT_EXCEEDED` error, but `/_synapse/client` is typically
        served by a different process."""
        self._assert_page_served()
