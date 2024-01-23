#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

from synapse.api.errors import SynapseError
from synapse.types import (
    RoomAlias,
    UserID,
    get_domain_from_id,
    get_localpart_from_id,
    map_username_to_mxid_localpart,
)

from tests import unittest


class IsMineIDTests(unittest.HomeserverTestCase):
    def test_is_mine_id(self) -> None:
        self.assertTrue(self.hs.is_mine_id("@user:test"))
        self.assertTrue(self.hs.is_mine_id("#room:test"))
        self.assertTrue(self.hs.is_mine_id("invalid:test"))

        self.assertFalse(self.hs.is_mine_id("@user:test\0"))
        self.assertFalse(self.hs.is_mine_id("@user"))

    def test_two_colons(self) -> None:
        """Test handling of IDs containing more than one colon."""
        # The domain starts after the first colon.
        # These functions must interpret things consistently.
        self.assertFalse(self.hs.is_mine_id("@user:test:test"))
        self.assertEqual("user", get_localpart_from_id("@user:test:test"))
        self.assertEqual("test:test", get_domain_from_id("@user:test:test"))


class UserIDTestCase(unittest.HomeserverTestCase):
    def test_parse(self) -> None:
        user = UserID.from_string("@1234abcd:test")

        self.assertEqual("1234abcd", user.localpart)
        self.assertEqual("test", user.domain)
        self.assertEqual(True, self.hs.is_mine(user))

    def test_parse_rejects_empty_id(self) -> None:
        with self.assertRaises(SynapseError):
            UserID.from_string("")

    def test_parse_rejects_missing_sigil(self) -> None:
        with self.assertRaises(SynapseError):
            UserID.from_string("alice:example.com")

    def test_parse_rejects_missing_separator(self) -> None:
        with self.assertRaises(SynapseError):
            UserID.from_string("@alice.example.com")

    def test_validation_rejects_missing_domain(self) -> None:
        self.assertFalse(UserID.is_valid("@alice:"))

    def test_build(self) -> None:
        user = UserID("5678efgh", "my.domain")

        self.assertEqual(user.to_string(), "@5678efgh:my.domain")

    def test_compare(self) -> None:
        userA = UserID.from_string("@userA:my.domain")
        userAagain = UserID.from_string("@userA:my.domain")
        userB = UserID.from_string("@userB:my.domain")

        self.assertTrue(userA == userAagain)
        self.assertTrue(userA != userB)


class RoomAliasTestCase(unittest.HomeserverTestCase):
    def test_parse(self) -> None:
        room = RoomAlias.from_string("#channel:test")

        self.assertEqual("channel", room.localpart)
        self.assertEqual("test", room.domain)
        self.assertEqual(True, self.hs.is_mine(room))

    def test_build(self) -> None:
        room = RoomAlias("channel", "my.domain")

        self.assertEqual(room.to_string(), "#channel:my.domain")

    def test_validate(self) -> None:
        id_string = "#test:domain,test"
        self.assertFalse(RoomAlias.is_valid(id_string))


class MapUsernameTestCase(unittest.TestCase):
    def test_pass_througuh(self) -> None:
        self.assertEqual(map_username_to_mxid_localpart("test1234"), "test1234")

    def test_upper_case(self) -> None:
        self.assertEqual(map_username_to_mxid_localpart("tEST_1234"), "test_1234")
        self.assertEqual(
            map_username_to_mxid_localpart("tEST_1234", case_sensitive=True),
            "t_e_s_t__1234",
        )

    def test_symbols(self) -> None:
        self.assertEqual(
            map_username_to_mxid_localpart("test=$?_1234"), "test=3d=24=3f_1234"
        )

    def test_leading_underscore(self) -> None:
        self.assertEqual(map_username_to_mxid_localpart("_test_1234"), "=5ftest_1234")

    def test_non_ascii(self) -> None:
        # this should work with either a unicode or a bytes
        self.assertEqual(map_username_to_mxid_localpart("têst"), "t=c3=aast")
        self.assertEqual(map_username_to_mxid_localpart("têst".encode()), "t=c3=aast")
