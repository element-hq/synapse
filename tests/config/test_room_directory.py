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

from synapse.config.room_directory import RoomDirectoryConfig

from tests import unittest


class RoomDirectoryConfigTestCase(unittest.TestCase):
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

        rd_config = RoomDirectoryConfig()
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

        rd_config = RoomDirectoryConfig()
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
