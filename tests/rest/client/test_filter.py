#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import Codes
from synapse.rest.client import filter
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest

PATH_PREFIX = "/_matrix/client/v2_alpha"


class FilterTestCase(unittest.HomeserverTestCase):
    user_id = "@apple:test"
    hijack_auth = True
    EXAMPLE_FILTER = {"room": {"timeline": {"types": ["m.room.message"]}}}
    EXAMPLE_FILTER_JSON = b'{"room": {"timeline": {"types": ["m.room.message"]}}}'
    servlets = [filter.register_servlets]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.filtering = hs.get_filtering()
        self.store = hs.get_datastores().main

    def test_add_filter(self) -> None:
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/user/%s/filter" % (self.user_id),
            self.EXAMPLE_FILTER_JSON,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"filter_id": "0"})
        filter = self.get_success(
            self.store.get_user_filter(
                user_id=UserID.from_string(FilterTestCase.user_id), filter_id=0
            )
        )
        self.pump()
        self.assertEqual(filter, self.EXAMPLE_FILTER)

    def test_add_filter_for_other_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/user/%s/filter" % ("@watermelon:test"),
            self.EXAMPLE_FILTER_JSON,
        )

        self.assertEqual(channel.code, 403)
        self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)

    def test_add_filter_non_local_user(self) -> None:
        _is_mine = self.hs.is_mine
        self.hs.is_mine = lambda target_user: False  # type: ignore[assignment]
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/user/%s/filter" % (self.user_id),
            self.EXAMPLE_FILTER_JSON,
        )

        self.hs.is_mine = _is_mine  # type: ignore[method-assign]
        self.assertEqual(channel.code, 403)
        self.assertEqual(channel.json_body["errcode"], Codes.FORBIDDEN)

    def test_get_filter(self) -> None:
        filter_id = self.get_success(
            self.filtering.add_user_filter(
                user_id=UserID.from_string("@apple:test"),
                user_filter=self.EXAMPLE_FILTER,
            )
        )
        self.reactor.advance(1)
        channel = self.make_request(
            "GET", "/_matrix/client/r0/user/%s/filter/%s" % (self.user_id, filter_id)
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, self.EXAMPLE_FILTER)

    def test_get_filter_non_existant(self) -> None:
        channel = self.make_request(
            "GET", "/_matrix/client/r0/user/%s/filter/12382148321" % (self.user_id)
        )

        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], Codes.NOT_FOUND)

    # Currently invalid params do not have an appropriate errcode
    # in errors.py
    def test_get_filter_invalid_id(self) -> None:
        channel = self.make_request(
            "GET", "/_matrix/client/r0/user/%s/filter/foobar" % (self.user_id)
        )

        self.assertEqual(channel.code, 400)

    # No ID also returns an invalid_id error
    def test_get_filter_no_id(self) -> None:
        channel = self.make_request(
            "GET", "/_matrix/client/r0/user/%s/filter/" % (self.user_id)
        )

        self.assertEqual(channel.code, 400)
