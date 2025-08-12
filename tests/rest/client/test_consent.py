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

import os
from http import HTTPStatus

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.urls import ConsentURIBuilder
from synapse.rest.client import login, room
from synapse.rest.consent import consent_resource
from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest
from tests.server import FakeSite, make_request


class ConsentResourceTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]
    user_id = True
    hijack_auth = False

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["form_secret"] = "123abc"

        # Make some temporary templates...
        temp_consent_path = self.mktemp()
        os.mkdir(temp_consent_path)
        os.mkdir(os.path.join(temp_consent_path, "en"))

        config["user_consent"] = {
            "version": "1",
            "template_dir": os.path.abspath(temp_consent_path),
        }

        with open(os.path.join(temp_consent_path, "en/1.html"), "w") as f:
            f.write("{{version}},{{has_consented}}")

        with open(os.path.join(temp_consent_path, "en/success.html"), "w") as f:
            f.write("yay!")

        hs = self.setup_test_homeserver(config=config)
        return hs

    def test_render_public_consent(self) -> None:
        """You can observe the terms form without specifying a user"""
        resource = consent_resource.ConsentResource(self.hs)
        channel = make_request(
            self.reactor,
            FakeSite(resource, self.reactor),
            "GET",
            "/consent?v=1",
            shorthand=False,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

    def test_accept_consent(self) -> None:
        """
        A user can use the consent form to accept the terms.
        """
        uri_builder = ConsentURIBuilder(self.hs.config)
        resource = consent_resource.ConsentResource(self.hs)

        # Register a user
        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        # Fetch the consent page, to get the consent version
        consent_uri = (
            uri_builder.build_user_consent_uri(user_id).replace("_matrix/", "")
            + "&u=user"
        )
        channel = make_request(
            self.reactor,
            FakeSite(resource, self.reactor),
            "GET",
            consent_uri,
            access_token=access_token,
            shorthand=False,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Get the version from the body, and whether we've consented
        version, consented = channel.result["body"].decode("ascii").split(",")
        self.assertEqual(consented, "False")

        # POST to the consent page, saying we've agreed
        channel = make_request(
            self.reactor,
            FakeSite(resource, self.reactor),
            "POST",
            consent_uri + "&v=" + version,
            access_token=access_token,
            shorthand=False,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Fetch the consent page, to get the consent version -- it should have
        # changed
        channel = make_request(
            self.reactor,
            FakeSite(resource, self.reactor),
            "GET",
            consent_uri,
            access_token=access_token,
            shorthand=False,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Get the version from the body, and check that it's the version we
        # agreed to, and that we've consented to it.
        version, consented = channel.result["body"].decode("ascii").split(",")
        self.assertEqual(consented, "True")
        self.assertEqual(version, "1")
