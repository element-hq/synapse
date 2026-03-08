#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Mathieu Velten
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#
import base64
from typing import Any
from unittest.mock import Mock

from twisted.internet.defer import Deferred
from twisted.internet.testing import MemoryReactor
from twisted.web.http_headers import Headers

import synapse.rest.admin
from synapse.config.webpush import HAS_PYWEBPUSH
from synapse.logging.context import make_deferred_yieldable
from synapse.rest.client import login, push_rule, pusher, receipts, room, versions
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, skip_unless


class WebPushPusherTests(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        receipts.register_servlets,
        push_rule.register_servlets,
        pusher.register_servlets,
        versions.register_servlets,
    ]
    user_id = True
    hijack_auth = False

    def default_config(self) -> JsonDict:
        config = super().default_config()

        import py_vapid

        vapid = py_vapid.Vapid()
        vapid.generate_keys()

        config["webpush"] = {
            "enabled": True,
            "vapid_contact_email": "test@example.org",
            "vapid_private_key": vapid.private_pem().decode(),
        }
        config["experimental_features"] = {
            "msc4174_enabled": True,
        }
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.push_attempts: list[tuple[Deferred, str, Headers]] = []

        m = Mock()

        def request(method: str, uri: str, headers: Headers, **kwargs: Any) -> Deferred:
            d: Deferred = Deferred()
            self.push_attempts.append((d, uri, headers))
            return make_deferred_yieldable(d)

        m.request = request

        hs = self.setup_test_homeserver(proxied_blocklisted_http_client=m)

        return hs

    @skip_unless(HAS_PYWEBPUSH, "requires pywebpush")
    def test_sends_webpush(self) -> None:
        """
        The WebPush pusher will send a push for each message to a WebPush endpoint
        when configured to do so.
        """
        # Register the user who gets notified
        user_id = self.register_user("user", "pass")
        access_token = self.login("user", "pass")

        # Register the user who sends the message
        other_user_id = self.register_user("otheruser", "pass")
        other_access_token = self.login("otheruser", "pass")

        # Register the pusher
        user_tuple = self.get_success(
            self.hs.get_datastores().main.get_user_by_access_token(access_token)
        )
        assert user_tuple is not None
        device_id = user_tuple.device_id

        # pywebpush will use this key to encrypt the push message
        # so it needs to be a real key, not just a random string
        from py_vapid import ec, serialization

        server_key = ec.generate_private_key(ec.SECP256R1())
        crypto_key = server_key.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )

        self.get_success(
            self.hs.get_pusherpool().add_or_update_pusher(
                user_id=user_id,
                device_id=device_id,
                kind="webpush",
                app_id="m.webpush",
                app_display_name="WebPush Notifications",
                device_display_name="pushy push",
                pushkey=base64.urlsafe_b64encode(crypto_key).decode("utf-8"),
                lang=None,
                data={"url": "http://push.example.com/webpush", "auth": "auth"},
            )
        )

        # Create a room
        room = self.helper.create_room_as(user_id, tok=access_token)

        # The other user joins
        self.helper.join(room=room, user=other_user_id, tok=other_access_token)

        # The other user sends a message
        self.helper.send(room, body="Hi!", tok=other_access_token)

        # One push was attempted to be sent
        self.assertEqual(len(self.push_attempts), 1)
        self.assertEqual(self.push_attempts[0][1], "http://push.example.com/webpush")
