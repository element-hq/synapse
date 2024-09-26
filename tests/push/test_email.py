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
import email.message
import os
from http import HTTPStatus
from typing import Any, Dict, List, Sequence, Tuple

import attr
import pkg_resources
from parameterized import parameterized

from twisted.internet.defer import Deferred
from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.api.errors import Codes, SynapseError
from synapse.push.emailpusher import EmailPusher
from synapse.rest.client import login, room
from synapse.rest.synapse.client.unsubscribe import UnsubscribeResource
from synapse.server import HomeServer
from synapse.util import Clock

from tests.server import FakeSite, make_request
from tests.unittest import HomeserverTestCase


@attr.s(auto_attribs=True)
class _User:
    "Helper wrapper for user ID and access token"

    id: str
    token: str


class EmailPusherTests(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
    ]
    hijack_auth = False

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        config = self.default_config()
        config["email"] = {
            "enable_notifs": True,
            "template_dir": os.path.abspath(
                pkg_resources.resource_filename("synapse", "res/templates")
            ),
            "expiry_template_html": "notice_expiry.html",
            "expiry_template_text": "notice_expiry.txt",
            "notif_template_html": "notif_mail.html",
            "notif_template_text": "notif_mail.txt",
            "smtp_host": "127.0.0.1",
            "smtp_port": 20,
            "require_transport_security": False,
            "smtp_user": None,
            "smtp_pass": None,
            "app_name": "Matrix",
            "notif_from": "test@example.com",
            "riot_base_url": None,
        }
        config["public_baseurl"] = "http://aaa"

        hs = self.setup_test_homeserver(config=config)

        # List[Tuple[Deferred, args, kwargs]]
        self.email_attempts: List[Tuple[Deferred, Sequence, Dict]] = []

        def sendmail(*args: Any, **kwargs: Any) -> Deferred:
            # This mocks out synapse.reactor.send_email._sendmail.
            d: Deferred = Deferred()
            self.email_attempts.append((d, args, kwargs))
            return d

        hs.get_send_email_handler()._sendmail = sendmail  # type: ignore[assignment]

        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Register the user who gets notified
        self.user_id = self.register_user("user", "pass")
        self.access_token = self.login("user", "pass")

        # Register other users
        self.others = [
            _User(
                id=self.register_user("otheruser1", "pass"),
                token=self.login("otheruser1", "pass"),
            ),
            _User(
                id=self.register_user("otheruser2", "pass"),
                token=self.login("otheruser2", "pass"),
            ),
        ]

        # Register the pusher
        user_tuple = self.get_success(
            self.hs.get_datastores().main.get_user_by_access_token(self.access_token)
        )
        assert user_tuple is not None
        self.device_id = user_tuple.device_id

        # We need to add email to account before we can create a pusher.
        self.get_success(
            hs.get_datastores().main.user_add_threepid(
                self.user_id, "email", "a@example.com", 0, 0
            )
        )

        pusher = self.get_success(
            self.hs.get_pusherpool().add_or_update_pusher(
                user_id=self.user_id,
                device_id=self.device_id,
                kind="email",
                app_id="m.email",
                app_display_name="Email Notifications",
                device_display_name="a@example.com",
                pushkey="a@example.com",
                lang=None,
                data={},
            )
        )
        assert isinstance(pusher, EmailPusher)
        self.pusher = pusher

        self.auth_handler = hs.get_auth_handler()
        self.store = hs.get_datastores().main

    def test_need_validated_email(self) -> None:
        """Test that we can only add an email pusher if the user has validated
        their email.
        """
        with self.assertRaises(SynapseError) as cm:
            self.get_success_or_raise(
                self.hs.get_pusherpool().add_or_update_pusher(
                    user_id=self.user_id,
                    device_id=self.device_id,
                    kind="email",
                    app_id="m.email",
                    app_display_name="Email Notifications",
                    device_display_name="b@example.com",
                    pushkey="b@example.com",
                    lang=None,
                    data={},
                )
            )

        self.assertEqual(400, cm.exception.code)
        self.assertEqual(Codes.THREEPID_NOT_FOUND, cm.exception.errcode)

    def test_simple_sends_email(self) -> None:
        # Create a simple room with two users
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.invite(
            room=room, src=self.user_id, tok=self.access_token, targ=self.others[0].id
        )
        self.helper.join(room=room, user=self.others[0].id, tok=self.others[0].token)

        # The other user sends a single message.
        self.helper.send(room, body="Hi!", tok=self.others[0].token)

        # We should get emailed about that message
        self._check_for_mail()

        # The other user sends multiple messages.
        self.helper.send(room, body="Hi!", tok=self.others[0].token)
        self.helper.send(room, body="There!", tok=self.others[0].token)

        self._check_for_mail()

    @parameterized.expand([(False,), (True,)])
    def test_unsubscribe(self, use_post: bool) -> None:
        # Create a simple room with two users
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.invite(
            room=room, src=self.user_id, tok=self.access_token, targ=self.others[0].id
        )
        self.helper.join(room=room, user=self.others[0].id, tok=self.others[0].token)

        # The other user sends a single message.
        self.helper.send(room, body="Hi!", tok=self.others[0].token)

        # We should get emailed about that message
        args, kwargs = self._check_for_mail()

        # That email should contain an unsubscribe link in the body and header.
        msg: bytes = args[5]

        # Multipart: plain text, base 64 encoded; html, base 64 encoded
        multipart_msg = email.message_from_bytes(msg)

        # Extract the text (non-HTML) portion of the multipart Message,
        # as a Message.
        txt_message = multipart_msg.get_payload(i=0)
        assert isinstance(txt_message, email.message.Message)

        # Extract the actual bytes from the Message object, and decode them to a `str`.
        txt_bytes = txt_message.get_payload(decode=True)
        assert isinstance(txt_bytes, bytes)
        txt = txt_bytes.decode()

        # Do the same for the HTML portion of the multipart Message.
        html_message = multipart_msg.get_payload(i=1)
        assert isinstance(html_message, email.message.Message)
        html_bytes = html_message.get_payload(decode=True)
        assert isinstance(html_bytes, bytes)
        html = html_bytes.decode()

        self.assertIn("/_synapse/client/unsubscribe", txt)
        self.assertIn("/_synapse/client/unsubscribe", html)

        # The unsubscribe headers should exist.
        assert multipart_msg.get("List-Unsubscribe") is not None
        self.assertIsNotNone(multipart_msg.get("List-Unsubscribe-Post"))

        # Open the unsubscribe link.
        unsubscribe_link = multipart_msg["List-Unsubscribe"].strip("<>")
        unsubscribe_resource = UnsubscribeResource(self.hs)
        channel = make_request(
            self.reactor,
            FakeSite(unsubscribe_resource, self.reactor),
            "POST" if use_post else "GET",
            unsubscribe_link,
            shorthand=False,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)

        # Ensure the pusher was removed.
        pushers = list(
            self.get_success(
                self.hs.get_datastores().main.get_pushers_by(
                    {"user_name": self.user_id}
                )
            )
        )
        self.assertEqual(pushers, [])

    def test_invite_sends_email(self) -> None:
        # Create a room and invite the user to it
        room = self.helper.create_room_as(self.others[0].id, tok=self.others[0].token)
        self.helper.invite(
            room=room,
            src=self.others[0].id,
            tok=self.others[0].token,
            targ=self.user_id,
        )

        # We should get emailed about the invite
        self._check_for_mail()

    def test_invite_to_empty_room_sends_email(self) -> None:
        # Create a room and invite the user to it
        room = self.helper.create_room_as(self.others[0].id, tok=self.others[0].token)
        self.helper.invite(
            room=room,
            src=self.others[0].id,
            tok=self.others[0].token,
            targ=self.user_id,
        )

        # Then have the original user leave
        self.helper.leave(room, self.others[0].id, tok=self.others[0].token)

        # We should get emailed about the invite
        self._check_for_mail()

    def test_multiple_members_email(self) -> None:
        # We want to test multiple notifications, so we pause processing of push
        # while we send messages.
        self.pusher._pause_processing()

        # Create a simple room with multiple other users
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)

        for other in self.others:
            self.helper.invite(
                room=room, src=self.user_id, tok=self.access_token, targ=other.id
            )
            self.helper.join(room=room, user=other.id, tok=other.token)

        # The other users send some messages
        self.helper.send(room, body="Hi!", tok=self.others[0].token)
        self.helper.send(room, body="There!", tok=self.others[1].token)
        self.helper.send(room, body="There!", tok=self.others[1].token)

        # Nothing should have happened yet, as we're paused.
        assert not self.email_attempts

        self.pusher._resume_processing()

        # We should get emailed about those messages
        self._check_for_mail()

    def test_multiple_rooms(self) -> None:
        # We want to test multiple notifications from multiple rooms, so we pause
        # processing of push while we send messages.
        self.pusher._pause_processing()

        # Create a simple room with multiple other users
        rooms = [
            self.helper.create_room_as(self.user_id, tok=self.access_token),
            self.helper.create_room_as(self.user_id, tok=self.access_token),
        ]

        for r, other in zip(rooms, self.others):
            self.helper.invite(
                room=r, src=self.user_id, tok=self.access_token, targ=other.id
            )
            self.helper.join(room=r, user=other.id, tok=other.token)

        # The other users send some messages
        self.helper.send(rooms[0], body="Hi!", tok=self.others[0].token)
        self.helper.send(rooms[1], body="There!", tok=self.others[1].token)
        self.helper.send(rooms[1], body="There!", tok=self.others[1].token)

        # Nothing should have happened yet, as we're paused.
        assert not self.email_attempts

        self.pusher._resume_processing()

        # We should get emailed about those messages
        self._check_for_mail()

    def test_room_notifications_include_avatar(self) -> None:
        # Create a room and set its avatar.
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.send_state(
            room, "m.room.avatar", {"url": "mxc://DUMMY_MEDIA_ID"}, self.access_token
        )

        # Invite two other uses.
        for other in self.others:
            self.helper.invite(
                room=room, src=self.user_id, tok=self.access_token, targ=other.id
            )
            self.helper.join(room=room, user=other.id, tok=other.token)

        # The other users send some messages.
        # TODO It seems that two messages are required to trigger an email?
        self.helper.send(room, body="Alpha", tok=self.others[0].token)
        self.helper.send(room, body="Beta", tok=self.others[1].token)

        # We should get emailed about those messages
        args, kwargs = self._check_for_mail()

        # That email should contain the room's avatar
        msg: bytes = args[5]
        # Multipart: plain text, base 64 encoded; html, base 64 encoded

        # Extract the html Message object from the Multipart Message.
        # We need the asserts to convince mypy that this is OK.
        html_message = email.message_from_bytes(msg).get_payload(i=1)
        assert isinstance(html_message, email.message.Message)

        # Extract the `bytes` from the html Message object, and decode to a `str`.
        html = html_message.get_payload(decode=True)
        assert isinstance(html, bytes)
        html = html.decode()

        self.assertIn("_matrix/media/v1/thumbnail/DUMMY_MEDIA_ID", html)

    def test_empty_room(self) -> None:
        """All users leaving a room shouldn't cause the pusher to break."""
        # Create a simple room with two users
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.invite(
            room=room, src=self.user_id, tok=self.access_token, targ=self.others[0].id
        )
        self.helper.join(room=room, user=self.others[0].id, tok=self.others[0].token)

        # The other user sends a single message.
        self.helper.send(room, body="Hi!", tok=self.others[0].token)

        # Leave the room before the message is processed.
        self.helper.leave(room, self.user_id, tok=self.access_token)
        self.helper.leave(room, self.others[0].id, tok=self.others[0].token)

        # We should get emailed about that message
        self._check_for_mail()

    def test_empty_room_multiple_messages(self) -> None:
        """All users leaving a room shouldn't cause the pusher to break."""
        # Create a simple room with two users
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.invite(
            room=room, src=self.user_id, tok=self.access_token, targ=self.others[0].id
        )
        self.helper.join(room=room, user=self.others[0].id, tok=self.others[0].token)

        # The other user sends a single message.
        self.helper.send(room, body="Hi!", tok=self.others[0].token)
        self.helper.send(room, body="There!", tok=self.others[0].token)

        # Leave the room before the message is processed.
        self.helper.leave(room, self.user_id, tok=self.access_token)
        self.helper.leave(room, self.others[0].id, tok=self.others[0].token)

        # We should get emailed about that message
        self._check_for_mail()

    def test_encrypted_message(self) -> None:
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.invite(
            room=room, src=self.user_id, tok=self.access_token, targ=self.others[0].id
        )
        self.helper.join(room=room, user=self.others[0].id, tok=self.others[0].token)

        # The other user sends some messages
        self.helper.send_event(room, "m.room.encrypted", {}, tok=self.others[0].token)

        # We should get emailed about that message
        self._check_for_mail()

    def test_no_email_sent_after_removed(self) -> None:
        # Create a simple room with two users
        room = self.helper.create_room_as(self.user_id, tok=self.access_token)
        self.helper.invite(
            room=room,
            src=self.user_id,
            tok=self.access_token,
            targ=self.others[0].id,
        )
        self.helper.join(
            room=room,
            user=self.others[0].id,
            tok=self.others[0].token,
        )

        # The other user sends a single message.
        self.helper.send(room, body="Hi!", tok=self.others[0].token)

        # We should get emailed about that message
        self._check_for_mail()

        # disassociate the user's email address
        self.get_success(
            self.auth_handler.delete_local_threepid(
                user_id=self.user_id, medium="email", address="a@example.com"
            )
        )

        # check that the pusher for that email address has been deleted
        pushers = list(
            self.get_success(
                self.hs.get_datastores().main.get_pushers_by(
                    {"user_name": self.user_id}
                )
            )
        )
        self.assertEqual(len(pushers), 0)

    def test_remove_unlinked_pushers_background_job(self) -> None:
        """Checks that all existing pushers associated with unlinked email addresses are removed
        upon running the remove_deleted_email_pushers background update.
        """
        # disassociate the user's email address manually (without deleting the pusher).
        # This resembles the old behaviour, which the background update below is intended
        # to clean up.
        self.get_success(
            self.hs.get_datastores().main.user_delete_threepid(
                self.user_id, "email", "a@example.com"
            )
        )

        # Run the "remove_deleted_email_pushers" background job
        self.get_success(
            self.hs.get_datastores().main.db_pool.simple_insert(
                table="background_updates",
                values={
                    "update_name": "remove_deleted_email_pushers",
                    "progress_json": "{}",
                    "depends_on": None,
                },
            )
        )

        # ... and tell the DataStore that it hasn't finished all updates yet
        self.hs.get_datastores().main.db_pool.updates._all_done = False

        # Now let's actually drive the updates to completion
        self.wait_for_background_updates()

        # Check that all pushers with unlinked addresses were deleted
        pushers = list(
            self.get_success(
                self.hs.get_datastores().main.get_pushers_by(
                    {"user_name": self.user_id}
                )
            )
        )
        self.assertEqual(len(pushers), 0)

    def _check_for_mail(self) -> Tuple[Sequence, Dict]:
        """
        Assert that synapse sent off exactly one email notification.

        Returns:
            args and kwargs passed to synapse.reactor.send_email._sendmail for
            that notification.
        """
        # Get the stream ordering before it gets sent
        pushers = list(
            self.get_success(
                self.hs.get_datastores().main.get_pushers_by(
                    {"user_name": self.user_id}
                )
            )
        )
        self.assertEqual(len(pushers), 1)
        last_stream_ordering = pushers[0].last_stream_ordering

        # Advance time a bit, so the pusher will register something has happened
        self.pump(10)

        # It hasn't succeeded yet, so the stream ordering shouldn't have moved
        pushers = list(
            self.get_success(
                self.hs.get_datastores().main.get_pushers_by(
                    {"user_name": self.user_id}
                )
            )
        )
        self.assertEqual(len(pushers), 1)
        self.assertEqual(last_stream_ordering, pushers[0].last_stream_ordering)

        # One email was attempted to be sent
        self.assertEqual(len(self.email_attempts), 1)

        deferred, sendmail_args, sendmail_kwargs = self.email_attempts[0]
        # Make the email succeed
        deferred.callback(True)
        self.pump()

        # One email was attempted to be sent
        self.assertEqual(len(self.email_attempts), 1)

        # The stream ordering has increased
        pushers = list(
            self.get_success(
                self.hs.get_datastores().main.get_pushers_by(
                    {"user_name": self.user_id}
                )
            )
        )
        self.assertEqual(len(pushers), 1)
        self.assertTrue(pushers[0].last_stream_ordering > last_stream_ordering)

        # Reset the attempts.
        self.email_attempts = []
        return sendmail_args, sendmail_kwargs
