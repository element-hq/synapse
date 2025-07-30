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
from typing import List, Optional, Tuple

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.storage._base import db_to_json
from synapse.storage.database import LoggingTransaction
from synapse.types import JsonDict
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class EndToEndKeyWorkerStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def test_get_master_cross_signing_key_updatable_before(self) -> None:
        # Should return False, None when there is no master key.
        alice = "@alice:test"
        exists, timestamp = self.get_success(
            self.store.get_master_cross_signing_key_updatable_before(alice)
        )
        self.assertIs(exists, False)
        self.assertIsNone(timestamp)

        # Upload a master key.
        dummy_key = {"keys": {"a": "b"}}
        self.get_success(
            self.store.set_e2e_cross_signing_key(alice, "master", dummy_key)
        )

        # Should now find that the key exists.
        exists, timestamp = self.get_success(
            self.store.get_master_cross_signing_key_updatable_before(alice)
        )
        self.assertIs(exists, True)
        self.assertIsNone(timestamp)

        # Write an updateable_before timestamp.
        written_timestamp = self.get_success(
            self.store.allow_master_cross_signing_key_replacement_without_uia(
                alice, 1000
            )
        )

        # Should now find that the key exists.
        exists, timestamp = self.get_success(
            self.store.get_master_cross_signing_key_updatable_before(alice)
        )
        self.assertIs(exists, True)
        self.assertEqual(timestamp, written_timestamp)

    def test_master_replacement_only_applies_to_latest_master_key(
        self,
    ) -> None:
        """We shouldn't allow updates w/o UIA to old master keys or other key types."""
        alice = "@alice:test"
        # Upload two master keys.
        key1 = {"keys": {"a": "b"}}
        key2 = {"keys": {"c": "d"}}
        key3 = {"keys": {"e": "f"}}
        self.get_success(self.store.set_e2e_cross_signing_key(alice, "master", key1))
        self.get_success(self.store.set_e2e_cross_signing_key(alice, "other", key2))
        self.get_success(self.store.set_e2e_cross_signing_key(alice, "master", key3))

        # Third key should be the current one.
        key = self.get_success(
            self.store.get_e2e_cross_signing_key(alice, "master", alice)
        )
        self.assertEqual(key, key3)

        timestamp = self.get_success(
            self.store.allow_master_cross_signing_key_replacement_without_uia(
                alice, 1000
            )
        )
        assert timestamp is not None

        def check_timestamp_column(
            txn: LoggingTransaction,
        ) -> List[Tuple[JsonDict, Optional[int]]]:
            """Fetch all rows for Alice's keys."""
            txn.execute(
                """
                SELECT keydata, updatable_without_uia_before_ms
                FROM e2e_cross_signing_keys
                WHERE user_id = ?
                ORDER BY stream_id ASC;
            """,
                (alice,),
            )
            return [(db_to_json(keydata), ts) for keydata, ts in txn.fetchall()]

        values = self.get_success(
            self.store.db_pool.runInteraction(
                "check_timestamp_column",
                check_timestamp_column,
            )
        )
        self.assertEqual(
            values,
            [
                (key1, None),
                (key2, None),
                (key3, timestamp),
            ],
        )
