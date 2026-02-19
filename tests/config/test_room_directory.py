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
import yaml

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
import synapse.rest.client.login
import synapse.rest.client.room
from synapse.config._base import RootConfig
from synapse.config.room_directory import RoomDirectoryConfig
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config


class RoomDirectoryConfigTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    servlets = [
        synapse.rest.admin.register_servlets,
        synapse.rest.client.login.register_servlets,
        synapse.rest.client.room.register_servlets,
    ]

    def test_alias_creation_acl(self) -> None:
        config = yaml.safe_load(
            """
        alias_creation_rules:
            - user_id: "*bob*"
              alias: "*"
              action: "deny"
            - user_id: "*"
              alias: "#unofficial_*"
              action: "allow"
            - user_id: "@foo*:example.com"
              alias: "*"
              action: "allow"
            - user_id: "@gah:example.com"
              alias: "#goo:example.com"
              action: "allow"

        room_list_publication_rules: []
        """
        )

        rd_config = RoomDirectoryConfig(RootConfig())
        rd_config.read_config(config)

        self.assertFalse(
            rd_config.is_alias_creation_allowed(
                user_id="@bob:example.com", room_id="!test", alias="#test:example.com"
            )
        )

        self.assertTrue(
            rd_config.is_alias_creation_allowed(
                user_id="@test:example.com",
                room_id="!test",
                alias="#unofficial_st:example.com",
            )
        )

        self.assertTrue(
            rd_config.is_alias_creation_allowed(
                user_id="@foobar:example.com",
                room_id="!test",
                alias="#test:example.com",
            )
        )

        self.assertTrue(
            rd_config.is_alias_creation_allowed(
                user_id="@gah:example.com", room_id="!test", alias="#goo:example.com"
            )
        )

        self.assertFalse(
            rd_config.is_alias_creation_allowed(
                user_id="@test:example.com", room_id="!test", alias="#test:example.com"
            )
        )

    def test_room_publish_acl(self) -> None:
        config = yaml.safe_load(
            """
        alias_creation_rules: []

        room_list_publication_rules:
            - user_id: "*bob*"
              alias: "*"
              action: "deny"
            - user_id: "*"
              alias: "#unofficial_*"
              action: "allow"
            - user_id: "@foo*:example.com"
              alias: "*"
              action: "allow"
            - user_id: "@gah:example.com"
              alias: "#goo:example.com"
              action: "allow"
            - room_id: "!test-deny"
              action: "deny"
        """
        )

        rd_config = RoomDirectoryConfig(RootConfig())
        rd_config.read_config(config)

        self.assertFalse(
            rd_config.is_publishing_room_allowed(
                user_id="@bob:example.com",
                room_id="!test",
                aliases=["#test:example.com"],
            )
        )

        self.assertTrue(
            rd_config.is_publishing_room_allowed(
                user_id="@test:example.com",
                room_id="!test",
                aliases=["#unofficial_st:example.com"],
            )
        )

        self.assertTrue(
            rd_config.is_publishing_room_allowed(
                user_id="@foobar:example.com", room_id="!test", aliases=[]
            )
        )

        self.assertTrue(
            rd_config.is_publishing_room_allowed(
                user_id="@gah:example.com",
                room_id="!test",
                aliases=["#goo:example.com"],
            )
        )

        self.assertFalse(
            rd_config.is_publishing_room_allowed(
                user_id="@test:example.com",
                room_id="!test",
                aliases=["#test:example.com"],
            )
        )

        self.assertTrue(
            rd_config.is_publishing_room_allowed(
                user_id="@foobar:example.com", room_id="!test-deny", aliases=[]
            )
        )

        self.assertFalse(
            rd_config.is_publishing_room_allowed(
                user_id="@gah:example.com", room_id="!test-deny", aliases=[]
            )
        )

        self.assertTrue(
            rd_config.is_publishing_room_allowed(
                user_id="@test:example.com",
                room_id="!test",
                aliases=["#unofficial_st:example.com", "#blah:example.com"],
            )
        )

    @override_config({"room_list_publication_rules": []})
    def test_room_creation_when_publishing_denied(self) -> None:
        """
        Test that when room publishing is denied via the config that new rooms can
        still be created and that the newly created room is not public.
        """

        user = self.register_user("alice", "pass")
        token = self.login("alice", "pass")
        room_id = self.helper.create_room_as(user, is_public=True, tok=token)

        res = self.get_success(self.store.get_room(room_id))
        assert res is not None
        is_public, _ = res

        self.assertFalse(is_public)
