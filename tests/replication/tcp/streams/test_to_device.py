#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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

import synapse
from synapse.replication.tcp.streams._base import (
    _STREAM_UPDATE_TARGET_ROW_COUNT,
    ToDeviceStream,
)
from synapse.types import JsonDict

from tests.replication._base import BaseStreamTestCase

logger = logging.getLogger(__name__)


class ToDeviceStreamTestCase(BaseStreamTestCase):
    servlets = [
        synapse.rest.admin.register_servlets,
        synapse.rest.client.login.register_servlets,
    ]

    def test_to_device_stream(self) -> None:
        store = self.hs.get_datastores().main

        user1 = self.register_user("user1", "pass")
        self.login("user1", "pass", "device")
        user2 = self.register_user("user2", "pass")
        self.login("user2", "pass", "device")

        # connect to pull the updates related to users creation/login
        self.reconnect()
        self.replicate()
        self.test_handler.received_rdata_rows.clear()
        # disconnect so we can accumulate the updates without pulling them
        self.disconnect()

        msg: JsonDict = {}
        msg["sender"] = "@sender:example.org"
        msg["type"] = "m.new_device"

        # add messages to the device inbox for user1 up until the
        # limit defined for a stream update batch
        for i in range(_STREAM_UPDATE_TARGET_ROW_COUNT):
            msg["content"] = {"device": {}}
            messages = {user1: {"device": msg}}

            self.get_success(
                store.add_messages_from_remote_to_device_inbox(
                    "example.org",
                    f"{i}",
                    messages,
                )
            )

        # add one more message, for user2 this time
        # this message would be dropped before fixing https://github.com/matrix-org/synapse/issues/15335
        msg["content"] = {"device": {}}
        messages = {user2: {"device": msg}}

        self.get_success(
            store.add_messages_from_remote_to_device_inbox(
                "example.org",
                f"{_STREAM_UPDATE_TARGET_ROW_COUNT}",
                messages,
            )
        )

        # replication is disconnected so we shouldn't get any updates yet
        received_to_device_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == ToDeviceStream.NAME
        ]
        self.assertEqual([], received_to_device_rows)

        # now reconnect to pull the updates
        self.reconnect()
        self.replicate()

        # we should receive the fact that we have to_device updates
        # for user1 and user2
        received_to_device_rows = [
            row
            for row in self.test_handler.received_rdata_rows
            if row[0] == ToDeviceStream.NAME
        ]
        self.assertEqual(
            len(received_to_device_rows),
            2,
            "Expected two rows in the to_device stream",
        )
        self.assertEqual(received_to_device_rows[0][2].entity, user1)
        self.assertEqual(received_to_device_rows[1][2].entity, user2)
