#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from synapse.replication.tcp.streams._base import MemberInfoChangesStream

from tests.replication._base import BaseStreamTestCase


class MemberInfoChangesStreamTestCase(BaseStreamTestCase):
    """MSC4428: the member info changes stream reaches workers."""

    def test_changes_are_replicated(self) -> None:
        store = self.hs.get_datastores().main
        worker_store = self.worker_hs.get_datastores().main

        self.get_success(
            store.add_member_info_changes(
                {
                    "!a:test": ["@erased:test", "@other:test"],
                    "!b:test": ["@erased:test"],
                }
            )
        )

        # Not yet connected: no rows should yet have been received
        self.assertEqual([], self.test_handler.received_rdata_rows)

        self.reconnect()
        self.replicate()

        received = [
            (token, row)
            for stream_name, token, row in self.test_handler.received_rdata_rows
            if stream_name == MemberInfoChangesStream.NAME
        ]
        self.assertEqual(
            [(row.room_id, row.stable_id) for _, row in received],
            [
                ("!a:test", "@erased:test"),
                ("!a:test", "@other:test"),
                ("!b:test", "@erased:test"),
            ],
        )
        tokens = [token for token, _ in received]
        self.assertEqual(tokens, sorted(tokens))

        # The worker's view of the stream position has caught up.
        self.assertEqual(
            worker_store.get_member_info_changes_stream_token(),
            store.get_member_info_changes_stream_token(),
        )
