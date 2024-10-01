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
from typing import Tuple
from unittest.mock import AsyncMock, Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EventTypes, LimitBlockingTypes, ServerNoticeMsgType
from synapse.api.errors import ResourceLimitError
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.server_notices.resource_limits_server_notices import (
    ResourceLimitsServerNotices,
)
from synapse.server_notices.server_notices_sender import ServerNoticesSender
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.unittest import override_config
from tests.utils import default_config


class TestResourceLimitsServerNotices(unittest.HomeserverTestCase):
    def default_config(self) -> JsonDict:
        config = default_config("test")

        config.update(
            {
                "admin_contact": "mailto:user@test.com",
                "limit_usage_by_mau": True,
                "server_notices": {
                    "system_mxid_localpart": "server",
                    "system_mxid_display_name": "test display name",
                    "system_mxid_avatar_url": None,
                    "room_name": "Server Notices",
                },
            }
        )

        # apply any additional config which was specified via the override_config
        # decorator.
        if self._extra_config is not None:
            config.update(self._extra_config)

        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        server_notices_sender = self.hs.get_server_notices_sender()
        assert isinstance(server_notices_sender, ServerNoticesSender)

        # relying on [1] is far from ideal, but the only case where
        # ResourceLimitsServerNotices class needs to be isolated is this test,
        # general code should never have a reason to do so ...
        rlsn = list(server_notices_sender._server_notices)[1]
        assert isinstance(rlsn, ResourceLimitsServerNotices)
        self._rlsn = rlsn

        self._rlsn._store.user_last_seen_monthly_active = AsyncMock(return_value=1000)
        self._rlsn._server_notices_manager.send_notice = AsyncMock(  # type: ignore[method-assign]
            return_value=Mock()
        )
        self._send_notice = self._rlsn._server_notices_manager.send_notice

        self.user_id = "@user_id:test"

        self._rlsn._server_notices_manager.get_or_create_notice_room_for_user = (
            AsyncMock(return_value="!something:localhost")
        )
        self._rlsn._server_notices_manager.maybe_get_notice_room_for_user = AsyncMock(
            return_value="!something:localhost"
        )
        self._rlsn._store.add_tag_to_room = AsyncMock(return_value=None)  # type: ignore[method-assign]
        self._rlsn._store.get_tags_for_room = AsyncMock(return_value={})

    @override_config({"hs_disabled": True})
    def test_maybe_send_server_notice_disabled_hs(self) -> None:
        """If the HS is disabled, we should not send notices"""
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))
        self._send_notice.assert_not_called()

    @override_config({"limit_usage_by_mau": False})
    def test_maybe_send_server_notice_to_user_flag_off(self) -> None:
        """If mau limiting is disabled, we should not send notices"""
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))
        self._send_notice.assert_not_called()

    def test_maybe_send_server_notice_to_user_remove_blocked_notice(self) -> None:
        """Test when user has blocked notice, but should have it removed"""

        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        mock_event = Mock(
            type=EventTypes.Message, content={"msgtype": ServerNoticeMsgType}
        )
        self._rlsn._store.get_events = AsyncMock(  # type: ignore[method-assign]
            return_value={"123": mock_event}
        )
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))
        # Would be better to check the content, but once == remove blocking event
        maybe_get_notice_room_for_user = (
            self._rlsn._server_notices_manager.maybe_get_notice_room_for_user
        )
        assert isinstance(maybe_get_notice_room_for_user, Mock)
        maybe_get_notice_room_for_user.assert_called_once()
        self._send_notice.assert_called_once()

    def test_maybe_send_server_notice_to_user_remove_blocked_notice_noop(self) -> None:
        """
        Test when user has blocked notice, but notice ought to be there (NOOP)
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None,
            side_effect=ResourceLimitError(403, "foo"),
        )

        mock_event = Mock(
            type=EventTypes.Message, content={"msgtype": ServerNoticeMsgType}
        )
        self._rlsn._store.get_events = AsyncMock(  # type: ignore[method-assign]
            return_value={"123": mock_event}
        )

        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        self._send_notice.assert_not_called()

    def test_maybe_send_server_notice_to_user_add_blocked_notice(self) -> None:
        """
        Test when user does not have blocked notice, but should have one
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None,
            side_effect=ResourceLimitError(403, "foo"),
        )
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        # Would be better to check contents, but 2 calls == set blocking event
        self.assertEqual(self._send_notice.call_count, 2)

    def test_maybe_send_server_notice_to_user_add_blocked_notice_noop(self) -> None:
        """
        Test when user does not have blocked notice, nor should they (NOOP)
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )

        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        self._send_notice.assert_not_called()

    def test_maybe_send_server_notice_to_user_not_in_mau_cohort(self) -> None:
        """
        Test when user is not part of the MAU cohort - this should not ever
        happen - but ...
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None
        )
        self._rlsn._store.user_last_seen_monthly_active = AsyncMock(return_value=None)
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        self._send_notice.assert_not_called()

    @override_config({"mau_limit_alerting": False})
    def test_maybe_send_server_notice_when_alerting_suppressed_room_unblocked(
        self,
    ) -> None:
        """
        Test that when server is over MAU limit and alerting is suppressed, then
        an alert message is not sent into the room
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None,
            side_effect=ResourceLimitError(
                403, "foo", limit_type=LimitBlockingTypes.MONTHLY_ACTIVE_USER
            ),
        )
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        self.assertEqual(self._send_notice.call_count, 0)

    @override_config({"mau_limit_alerting": False})
    def test_check_hs_disabled_unaffected_by_mau_alert_suppression(self) -> None:
        """
        Test that when a server is disabled, that MAU limit alerting is ignored.
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None,
            side_effect=ResourceLimitError(
                403, "foo", limit_type=LimitBlockingTypes.HS_DISABLED
            ),
        )
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        # Would be better to check contents, but 2 calls == set blocking event
        self.assertEqual(self._send_notice.call_count, 2)

    @override_config({"mau_limit_alerting": False})
    def test_maybe_send_server_notice_when_alerting_suppressed_room_blocked(
        self,
    ) -> None:
        """
        When the room is already in a blocked state, test that when alerting
        is suppressed that the room is returned to an unblocked state.
        """
        self._rlsn._auth_blocking.check_auth_blocking = AsyncMock(  # type: ignore[method-assign]
            return_value=None,
            side_effect=ResourceLimitError(
                403, "foo", limit_type=LimitBlockingTypes.MONTHLY_ACTIVE_USER
            ),
        )

        self._rlsn._is_room_currently_blocked = AsyncMock(  # type: ignore[method-assign]
            return_value=(True, [])
        )

        mock_event = Mock(
            type=EventTypes.Message, content={"msgtype": ServerNoticeMsgType}
        )
        self._rlsn._store.get_events = AsyncMock(  # type: ignore[method-assign]
            return_value={"123": mock_event}
        )
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        self._send_notice.assert_called_once()


class TestResourceLimitsServerNoticesWithRealRooms(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        c = super().default_config()
        c["server_notices"] = {
            "system_mxid_localpart": "server",
            "system_mxid_display_name": None,
            "system_mxid_avatar_url": None,
            "room_name": "Test Server Notice Room",
        }
        c["limit_usage_by_mau"] = True
        c["max_mau_value"] = 5
        c["admin_contact"] = "mailto:user@test.com"
        return c

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.server_notices_manager = self.hs.get_server_notices_manager()
        self.event_source = self.hs.get_event_sources()

        server_notices_sender = self.hs.get_server_notices_sender()
        assert isinstance(server_notices_sender, ServerNoticesSender)

        # relying on [1] is far from ideal, but the only case where
        # ResourceLimitsServerNotices class needs to be isolated is this test,
        # general code should never have a reason to do so ...
        rlsn = list(server_notices_sender._server_notices)[1]
        assert isinstance(rlsn, ResourceLimitsServerNotices)
        self._rlsn = rlsn

        self.user_id = "@user_id:test"

    def test_server_notice_only_sent_once(self) -> None:
        self.store.get_monthly_active_count = AsyncMock(return_value=1000)

        self.store.user_last_seen_monthly_active = AsyncMock(return_value=1000)

        # Call the function multiple times to ensure we only send the notice once
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))
        self.get_success(self._rlsn.maybe_send_server_notice_to_user(self.user_id))

        # Now lets get the last load of messages in the service notice room and
        # check that there is only one server notice
        room_id = self.get_success(
            self.server_notices_manager.get_or_create_notice_room_for_user(self.user_id)
        )

        token = self.event_source.get_current_token()
        events, _ = self.get_success(
            self.store.get_recent_events_for_room(
                room_id, limit=100, end_token=token.room_key
            )
        )

        count = 0
        for event in events:
            if event.type != EventTypes.Message:
                continue
            if event.content.get("msgtype") != ServerNoticeMsgType:
                continue

            count += 1

        self.assertEqual(count, 1)

    def test_no_invite_without_notice(self) -> None:
        """Tests that a user doesn't get invited to a server notices room without a
        server notice being sent.

        The scenario for this test is a single user on a server where the MAU limit
        hasn't been reached (since it's the only user and the limit is 5), so users
        shouldn't receive a server notice.
        """
        m = AsyncMock(return_value=None)
        self._rlsn._server_notices_manager.maybe_get_notice_room_for_user = m

        user_id = self.register_user("user", "password")
        tok = self.login("user", "password")

        channel = self.make_request("GET", "/sync?timeout=0", access_token=tok)

        self.assertNotIn(
            "rooms", channel.json_body, "Got invites without server notice"
        )

        m.assert_called_once_with(user_id)

    def test_invite_with_notice(self) -> None:
        """Tests that, if the MAU limit is hit, the server notices user invites each user
        to a room in which it has sent a notice.
        """
        user_id, tok, room_id = self._trigger_notice_and_join()

        # Sync again to retrieve the events in the room, so we can check whether this
        # room has a notice in it.
        channel = self.make_request("GET", "/sync?timeout=0", access_token=tok)

        # Scan the events in the room to search for a message from the server notices
        # user.
        events = channel.json_body["rooms"]["join"][room_id]["timeline"]["events"]
        notice_in_room = False
        for event in events:
            if (
                event["type"] == EventTypes.Message
                and event["sender"] == self.hs.config.servernotices.server_notices_mxid
            ):
                notice_in_room = True

        self.assertTrue(notice_in_room, "No server notice in room")

    def _trigger_notice_and_join(self) -> Tuple[str, str, str]:
        """Creates enough active users to hit the MAU limit and trigger a system notice
        about it, then joins the system notices room with one of the users created.

        Returns:
            A tuple of:
                user_id: The ID of the user that joined the room.
                tok: The access token of the user that joined the room.
                room_id: The ID of the room that's been joined.
        """
        # We need at least one user to process
        self.assertGreater(self.hs.config.server.max_mau_value, 0)

        invites = {}

        # Register as many users as the MAU limit allows.
        for i in range(self.hs.config.server.max_mau_value):
            localpart = "user%d" % i
            user_id = self.register_user(localpart, "password")
            tok = self.login(localpart, "password")

            # Sync with the user's token to mark the user as active.
            channel = self.make_request(
                "GET",
                "/sync?timeout=0",
                access_token=tok,
            )

            # Also retrieves the list of invites for this user. We don't care about that
            # one except if we're processing the last user, which should have received an
            # invite to a room with a server notice about the MAU limit being reached.
            # We could also pick another user and sync with it, which would return an
            # invite to a system notices room, but it doesn't matter which user we're
            # using so we use the last one because it saves us an extra sync.
            if "rooms" in channel.json_body:
                invites = channel.json_body["rooms"]["invite"]

        # Make sure we have an invite to process.
        self.assertEqual(len(invites), 1, invites)

        # Join the room.
        room_id = list(invites.keys())[0]
        self.helper.join(room=room_id, user=user_id, tok=tok)

        return user_id, tok, room_id
