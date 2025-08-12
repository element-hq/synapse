#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from http import HTTPStatus

from twisted.test.proto_helpers import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, profile, room, thread_subscriptions
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest

PREFIX = "/_matrix/client/unstable/io.element.msc4306/rooms"


class ThreadSubscriptionsTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        profile.register_servlets,
        room.register_servlets,
        thread_subscriptions.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4306_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user_id = self.register_user("user", "password")
        self.token = self.login("user", "password")
        self.other_user_id = self.register_user("other_user", "password")
        self.other_token = self.login("other_user", "password")

        # Create a room and send a message to use as a thread root
        self.room_id = self.helper.create_room_as(self.user_id, tok=self.token)
        self.helper.join(self.room_id, self.other_user_id, tok=self.other_token)
        response = self.helper.send(self.room_id, body="Root message", tok=self.token)
        self.root_event_id = response["event_id"]

        # Send a message in the thread
        self.helper.send_event(
            room_id=self.room_id,
            type="m.room.message",
            content={
                "body": "Thread message",
                "msgtype": "m.text",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": self.root_event_id,
                },
            },
            tok=self.token,
        )

    def test_get_thread_subscription_unsubscribed(self) -> None:
        """Test retrieving thread subscription when not subscribed."""
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_get_thread_subscription_nonexistent_thread(self) -> None:
        """Test retrieving subscription settings for a nonexistent thread."""
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/$nonexistent:example.org/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_get_thread_subscription_no_access(self) -> None:
        """Test that a user can't get thread subscription for a thread they can't access."""
        self.register_user("no_access", "password")
        no_access_token = self.login("no_access", "password")

        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=no_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_subscribe_manual_then_automatic(self) -> None:
        """Test subscribing to a thread, first a manual subscription then an automatic subscription.
        The manual subscription wins over the automatic one."""
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            {
                "automatic": False,
            },
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Assert the subscription was saved
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"automatic": False})

        # Now also register an automatic subscription; it should not
        # override the manual subscription
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            {"automatic": True},
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Assert the manual subscription was not overridden
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"automatic": False})

    def test_subscribe_automatic_then_manual(self) -> None:
        """Test subscribing to a thread, first an automatic subscription then a manual subscription.
        The manual subscription wins over the automatic one."""
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            {
                "automatic": True,
            },
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Assert the subscription was saved
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"automatic": True})

        # Now also register a manual subscription
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            {"automatic": False},
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Assert the manual subscription was not overridden
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"automatic": False})

    def test_unsubscribe(self) -> None:
        """Test subscribing to a thread, then unsubscribing."""
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            {
                "automatic": True,
            },
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Assert the subscription was saved
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)
        self.assertEqual(channel.json_body, {"automatic": True})

        # Now also register a manual subscription
        channel = self.make_request(
            "DELETE",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        # Assert the manual subscription was not overridden
        channel = self.make_request(
            "GET",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_set_thread_subscription_nonexistent_thread(self) -> None:
        """Test setting subscription settings for a nonexistent thread."""
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/$nonexistent:example.org/subscription",
            {"automatic": True},
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_set_thread_subscription_no_access(self) -> None:
        """Test that a user can't set thread subscription for a thread they can't access."""
        self.register_user("no_access2", "password")
        no_access_token = self.login("no_access2", "password")

        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            {"automatic": True},
            access_token=no_access_token,
        )
        self.assertEqual(channel.code, HTTPStatus.NOT_FOUND)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_invalid_body(self) -> None:
        """Test that sending invalid subscription settings is rejected."""
        channel = self.make_request(
            "PUT",
            f"{PREFIX}/{self.room_id}/thread/{self.root_event_id}/subscription",
            # non-boolean `automatic`
            {"automatic": "true"},
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST)
