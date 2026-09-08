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
import os
from http import HTTPStatus

from twisted.web.resource import Resource

import synapse
from synapse.api.errors import Codes
from synapse.api.urls import STATIC_PREFIX
from synapse.http.server import StaticResource

from tests import unittest


class ResourceTreeTestCase(unittest.HomeserverTestCase):
    servlets = []

    def create_resource_dict(self) -> dict[str, Resource]:
        """
        Register /_matrix/static for the test.
        """
        resources = super().create_resource_dict()
        resources[STATIC_PREFIX] = StaticResource(
            # as in `synapse/app/homeserver.py` `_configure_named_resource`
            os.path.join(os.path.dirname(synapse.__file__), "static")
        )
        return resources

    def test_inserted_segment_is_silently_swallowed(self) -> None:
        """
        Regression test for https://github.com/element-hq/synapse/security/advisories/GHSA-vh4c-pqh4-w3wq

        The path `/_matrix/INSERTED/static/client/login/style.css` used to resolve to the same
        as `/_matrix/static/client/login/style.css`.
        """
        PATH_SUFFIX = "/static/client/login/style.css"
        correct_channel = self.make_request(
            "GET",
            f"/_matrix{PATH_SUFFIX}",
            shorthand=False,
        )
        # The correct path should give a 200 OK static resource
        self.assertEqual(correct_channel.code, HTTPStatus.OK, correct_channel.result)

        wrong_channel = self.make_request(
            "GET",
            f"/_matrix/INSERTED{PATH_SUFFIX}",
            shorthand=False,
        )
        # This prefixed version of the same path should give a 404
        self.assertEqual(wrong_channel.code, HTTPStatus.NOT_FOUND, wrong_channel.result)
        self.assertEqual(
            wrong_channel.json_body["errcode"], Codes.UNRECOGNIZED, wrong_channel.result
        )
