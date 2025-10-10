#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import logging

from synapse.handlers.typing import RoomMember, TypingWriterHandler
from synapse.replication.tcp.streams import TypingStream
from synapse.util.caches.stream_change_cache import StreamChangeCache

from tests.replication._base import BaseStreamTestCase

logger = logging.getLogger(__name__)

USER_ID = "@feeling:blue"
USER_ID_2 = "@da-ba-dee:blue"

ROOM_ID = "!bar:blue"
ROOM_ID_2 = "!foo:blue"


class TypingStreamTestCase(BaseStreamTestCase):
    def test_typing(self) -> None:
        typing = self.hs.get_typing_handler()
        assert isinstance(typing, TypingWriterHandler)

        # Create a typing update before we reconnect so that there is a missing
        # update to fetch.
        typing._push_update(member=RoomMember(ROOM_ID, USER_ID), typing=True)

        # Not yet connected: no rows should yet have been received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # Reconnect
        self.reconnect()

        typing._push_update(member=RoomMember(ROOM_ID, USER_ID), typing=True)
        # Pull in the updates
        self.replicate()

        # We should now see an attempt to connect to the master
        request = self.handle_http_replication_attempt()
        self.assert_request_is_get_repl_stream_updates(request, TypingStream.NAME)

        # Filter the updates to only include typing changes
        received_typing_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == TypingStream.NAME
        ]
        self.assertEqual(
            len(received_typing_rows),
            1,
            "Expected exactly one row for the typing stream",
        )
        (stream_name, token, row) = received_typing_rows[0]
        self.assertEqual(stream_name, TypingStream.NAME)
        self.assertIsInstance(row, TypingStream.ROW_TYPE)
        self.assertEqual(row.room_id, ROOM_ID)
        self.assertEqual(row.user_ids, [USER_ID])
        # Clear out the received rows that we've checked so we can check for new ones later
        self.test_handler.received_rdata_rows.clear()

        # Now let's disconnect and insert some data.
        self.disconnect()

        typing._push_update(member=RoomMember(ROOM_ID, USER_ID), typing=False)

        # Not yet connected: no rows should yet have been received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        # Now reconnect and pull the updates
        self.reconnect()
        self.replicate()

        # We should now see an attempt to connect to the master
        request = self.handle_http_replication_attempt()
        self.assert_request_is_get_repl_stream_updates(request, TypingStream.NAME)

        # The from token should be the token from the last RDATA we got.
        assert request.args is not None
        self.assertEqual(int(request.args[b"from_token"][0]), token)

        received_typing_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == TypingStream.NAME
        ]
        self.assertEqual(
            len(received_typing_rows),
            1,
            "Expected exactly one row for the typing stream",
        )
        (stream_name, token, row) = received_typing_rows[0]
        self.assertEqual(stream_name, TypingStream.NAME)
        self.assertIsInstance(row, TypingStream.ROW_TYPE)
        self.assertEqual(row.room_id, ROOM_ID)
        self.assertEqual(row.user_ids, [])

    def test_reset(self) -> None:
        """
        Test what happens when a typing stream resets.

        This is emulated by jumping the stream ahead, then reconnecting (which
        sends the proper position and RDATA).
        """
        # FIXME: Because huge RDATA log line is triggered in this test,
        # trial breaks, sometimes (flakily) failing the test run.
        # ref: https://github.com/twisted/twisted/issues/12482
        # To remove this, we would need to fix the above issue and
        # update, including in olddeps (so several years' wait).
        server_logger = logging.getLogger("tests.server")
        server_logger_was_disabled = server_logger.disabled
        server_logger.disabled = True
        try:
            typing = self.hs.get_typing_handler()
            assert isinstance(typing, TypingWriterHandler)

            # Create a typing update before we reconnect so that there is a missing
            # update to fetch.
            typing._push_update(member=RoomMember(ROOM_ID, USER_ID), typing=True)

            # Not yet connected: no rows should yet have been received
            self.assertEqual([], self.test_handler.received_rdata_rows)

            # Now reconnect to pull the updates
            self.reconnect()

            typing._push_update(member=RoomMember(ROOM_ID, USER_ID), typing=True)
            # Pull in the updates
            self.replicate()

            # We should now see an attempt to connect to the master
            request = self.handle_http_replication_attempt()
            self.assert_request_is_get_repl_stream_updates(request, "typing")

            received_typing_rows = [
                row
                for row in self.test_handler.received_rdata_rows
                if row[0] == TypingStream.NAME
            ]
            self.assertEqual(
                len(received_typing_rows),
                1,
                "Expected exactly one row for the typing stream",
            )
            (stream_name, token, row) = received_typing_rows[0]
            self.assertEqual(stream_name, TypingStream.NAME)
            self.assertIsInstance(row, TypingStream.ROW_TYPE)
            self.assertEqual(row.room_id, ROOM_ID)
            self.assertEqual(row.user_ids, [USER_ID])

            # Push the stream forward a bunch so it can be reset.
            for i in range(100):
                typing._push_update(
                    member=RoomMember(ROOM_ID, "@test%s:blue" % i), typing=True
                )
            # Pull in the updates
            self.replicate()

            # Disconnect.
            self.disconnect()
            self.test_handler.received_rdata_rows.clear()

            # Reset the typing handler
            self.hs.get_replication_streams()["typing"].last_token = 0
            self.hs.get_replication_command_handler()._streams["typing"].last_token = 0
            typing._latest_room_serial = 0
            typing._typing_stream_change_cache = StreamChangeCache(
                name="TypingStreamChangeCache",
                server_name=self.hs.hostname,
                current_stream_pos=typing._latest_room_serial,
            )
            typing._reset()

            # Now reconnect and pull the updates
            self.reconnect()
            self.replicate()

            # We should now see an attempt to connect to the master
            request = self.handle_http_replication_attempt()
            self.assert_request_is_get_repl_stream_updates(request, "typing")

            # Push additional data.
            typing._push_update(member=RoomMember(ROOM_ID_2, USER_ID_2), typing=False)
            # Pull the updates
            self.replicate()

            received_typing_rows = [
                row
                for row in self.test_handler.received_rdata_rows
                if row[0] == TypingStream.NAME
            ]
            self.assertEqual(
                len(received_typing_rows),
                1,
                "Expected exactly one row for the typing stream",
            )
            (stream_name, token, row) = received_typing_rows[0]
            self.assertEqual(stream_name, TypingStream.NAME)
            self.assertIsInstance(row, TypingStream.ROW_TYPE)
            self.assertEqual(row.room_id, ROOM_ID_2)
            self.assertEqual(row.user_ids, [])
            # The token should have been reset.
            self.assertEqual(token, 1)
        finally:
            server_logger.disabled = server_logger_was_disabled
