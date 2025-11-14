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

from twisted.internet.testing import MemoryReactor

from synapse.appservice import ApplicationService
from synapse.rest import admin
from synapse.rest.client import directory, login, room
from synapse.server import HomeServer
from synapse.types import RoomAlias, UserID
from synapse.util.clock import Clock
from synapse.util.stringutils import random_string

from tests import unittest
from tests.unittest import override_config


class DirectoryTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        directory.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["require_membership_for_aliases"] = True

        self.hs = self.setup_test_homeserver(config=config)

        return self.hs

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        """Create two local users and access tokens for them.
        One of them creates a room."""
        self.room_owner = self.register_user("room_owner", "test")
        self.room_owner_tok = self.login("room_owner", "test")

        self.room_id = self.helper.create_room_as(
            self.room_owner, tok=self.room_owner_tok
        )

        self.user = self.register_user("user", "test")
        self.user_tok = self.login("user", "test")

    def test_state_event_not_in_room(self) -> None:
        self.ensure_user_left_room()
        self.set_alias_via_state_event(HTTPStatus.FORBIDDEN)

    def test_directory_endpoint_not_in_room(self) -> None:
        self.ensure_user_left_room()
        self.set_alias_via_directory(HTTPStatus.FORBIDDEN)

    def test_state_event_in_room_too_long(self) -> None:
        self.ensure_user_joined_room()
        self.set_alias_via_state_event(HTTPStatus.BAD_REQUEST, alias_length=256)

    def test_directory_in_room_too_long(self) -> None:
        self.ensure_user_joined_room()
        self.set_alias_via_directory(HTTPStatus.BAD_REQUEST, alias_length=256)

    @override_config({"default_room_version": 5})
    def test_state_event_user_in_v5_room(self) -> None:
        """Test that a regular user can add alias events before room v6"""
        self.ensure_user_joined_room()
        self.set_alias_via_state_event(HTTPStatus.OK)

    @override_config({"default_room_version": 6})
    def test_state_event_v6_room(self) -> None:
        """Test that a regular user can *not* add alias events from room v6"""
        self.ensure_user_joined_room()
        self.set_alias_via_state_event(HTTPStatus.FORBIDDEN)

    def test_directory_in_room(self) -> None:
        self.ensure_user_joined_room()
        self.set_alias_via_directory(HTTPStatus.OK)

    def test_room_creation_too_long(self) -> None:
        url = "/_matrix/client/r0/createRoom"

        # We use deliberately a localpart under the length threshold so
        # that we can make sure that the check is done on the whole alias.
        request_data = {"room_alias_name": random_string(256 - len(self.hs.hostname))}
        channel = self.make_request(
            "POST", url, request_data, access_token=self.user_tok
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)

    def test_room_creation(self) -> None:
        url = "/_matrix/client/r0/createRoom"

        # Check with an alias of allowed length. There should already be
        # a test that ensures it works in test_register.py, but let's be
        # as cautious as possible here.
        request_data = {"room_alias_name": random_string(5)}
        channel = self.make_request(
            "POST", url, request_data, access_token=self.user_tok
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_deleting_alias_via_directory(self) -> None:
        # Add an alias for the room. We must be joined to do so.
        self.ensure_user_joined_room()
        alias = self.set_alias_via_directory(HTTPStatus.OK)

        # Then try to remove the alias
        channel = self.make_request(
            "DELETE",
            f"/_matrix/client/r0/directory/room/{alias}",
            access_token=self.user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_deleting_alias_via_directory_appservice(self) -> None:
        user_id = "@as:test"
        as_token = "i_am_an_app_service"

        appservice = ApplicationService(
            as_token,
            id="1234",
            namespaces={"aliases": [{"regex": "#asns-*", "exclusive": True}]},
            sender=UserID.from_string(user_id),
        )
        self.hs.get_datastores().main.services_cache.append(appservice)

        # Add an alias for the room, as the appservice
        alias = RoomAlias(f"asns-{random_string(5)}", self.hs.hostname).to_string()
        request_data = {"room_id": self.room_id}

        channel = self.make_request(
            "PUT",
            f"/_matrix/client/r0/directory/room/{alias}",
            request_data,
            access_token=as_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

        # Then try to remove the alias, as the appservice
        channel = self.make_request(
            "DELETE",
            f"/_matrix/client/r0/directory/room/{alias}",
            access_token=as_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_deleting_nonexistant_alias(self) -> None:
        # Check that no alias exists
        alias = "#potato:test"
        channel = self.make_request(
            "GET",
            f"/_matrix/client/r0/directory/room/{alias}",
            access_token=self.user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.result)
        self.assertIn("error", channel.json_body, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND", channel.json_body)

        # Then try to remove the alias
        channel = self.make_request(
            "DELETE",
            f"/_matrix/client/r0/directory/room/{alias}",
            access_token=self.user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND, channel.result)
        self.assertIn("error", channel.json_body, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND", channel.json_body)

    def set_alias_via_state_event(
        self, expected_code: HTTPStatus, alias_length: int = 5
    ) -> None:
        url = "/_matrix/client/r0/rooms/%s/state/m.room.aliases/%s" % (
            self.room_id,
            self.hs.hostname,
        )

        request_data = {"aliases": [self.random_alias(alias_length)]}

        channel = self.make_request(
            "PUT", url, request_data, access_token=self.user_tok
        )
        self.assertEqual(channel.code, expected_code, channel.result)

    def set_alias_via_directory(
        self, expected_code: HTTPStatus, alias_length: int = 5
    ) -> str:
        alias = self.random_alias(alias_length)
        url = "/_matrix/client/r0/directory/room/%s" % alias
        request_data = {"room_id": self.room_id}

        channel = self.make_request(
            "PUT", url, request_data, access_token=self.user_tok
        )
        self.assertEqual(channel.code, expected_code, channel.result)
        return alias

    def test_invalid_alias(self) -> None:
        alias = "#potato"
        channel = self.make_request(
            "GET",
            f"/_matrix/client/r0/directory/room/{alias}",
            access_token=self.user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertIn("error", channel.json_body, channel.json_body)
        self.assertEqual(
            channel.json_body["errcode"], "M_INVALID_PARAM", channel.json_body
        )

    def random_alias(self, length: int) -> str:
        return RoomAlias(random_string(length), self.hs.hostname).to_string()

    def ensure_user_left_room(self) -> None:
        self.ensure_membership("leave")

    def ensure_user_joined_room(self) -> None:
        self.ensure_membership("join")

    def ensure_membership(self, membership: str) -> None:
        try:
            if membership == "leave":
                self.helper.leave(room=self.room_id, user=self.user, tok=self.user_tok)
            if membership == "join":
                self.helper.join(room=self.room_id, user=self.user, tok=self.user_tok)
        except AssertionError:
            # We don't care whether the leave request didn't return a 200 (e.g.
            # if the user isn't already in the room), because we only want to
            # make sure the user isn't in the room.
            pass
