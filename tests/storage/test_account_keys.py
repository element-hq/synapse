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
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#


from signedjson.key import get_verify_key
from unpaddedbase64 import encode_base64

from twisted.internet.testing import MemoryReactor

from synapse.server import HomeServer
from synapse.types import get_localpart_from_id
from synapse.util.clock import Clock

from tests import unittest


class AccountKeysTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.user = "@user:test"
        self.user2 = "@user2:test"

    def test_get_or_create_local_account_key_user_id(self) -> None:
        key_user_id, signing_key = self.get_success(
            self.store.get_or_create_local_account_key_user_id(self.user)
        )
        # asserts the localpart is unpadded urlsafe base64
        self.assertRegex(key_user_id, r"^@[A-Za-z0-9\-_]{43}:test$")
        # asserts the public key is the localpart
        self.assertEquals(
            encode_base64(get_verify_key(signing_key).encode(), urlsafe=True),
            get_localpart_from_id(key_user_id),
        )
        # asserts the key ID is 1
        self.assertEquals(signing_key.version, "1")
        # assert that repeated calls return the same key
        key_user_id2, key2 = self.get_success(
            self.store.get_or_create_local_account_key_user_id(self.user)
        )
        self.assertEquals(key_user_id, key_user_id2)
        self.assertEquals(signing_key.encode(), key2.encode())

        # assert that calling for a different user makes a different key
        key_user_id_2, signing_key_2 = self.get_success(
            self.store.get_or_create_local_account_key_user_id(self.user2)
        )
        self.assertNotEquals(signing_key.encode(), signing_key_2.encode())
        self.assertNotEquals(key_user_id, key_user_id_2)

    def test_get_account_name_user_ids_for_account_key_user_ids(self) -> None:
        key_user_id, _ = self.get_success(
            self.store.get_or_create_local_account_key_user_id(
                self.user,
            )
        )
        result = self.get_success(
            self.store.get_account_name_user_ids_for_account_key_user_ids(
                [key_user_id]
            ),
        )
        self.assertEquals(result[key_user_id], self.user)

    def test_get_account_name_user_ids_for_account_key_user_ids_multiple(self) -> None:
        key_user_id_alice, _ = self.get_success(
            self.store.get_or_create_local_account_key_user_id(
                "@alice:test",
            )
        )
        key_user_id_bob, _ = self.get_success(
            self.store.get_or_create_local_account_key_user_id(
                "@bob:test",
            )
        )
        key_user_id_unknown = "@6fey6W1wS3-vbvUmHZnTd6Gi3o-TIxvIcwtEQP4nrW0:test"
        result = self.get_success(
            self.store.get_account_name_user_ids_for_account_key_user_ids(
                [key_user_id_alice, key_user_id_bob, key_user_id_unknown]
            ),
        )
        self.assertEquals(result[key_user_id_alice], "@alice:test")
        self.assertEquals(result[key_user_id_bob], "@bob:test")
        self.assertEquals(result.get(key_user_id_unknown, None), None)

    def test_store_verified_account_name_user_ids(self) -> None:
        local_key_user_id = "@6fey6W1wS3-vbvUmHZnTd6Gi3o-TIxvIcwtEQP4nrW0:test"
        local_name_user_id = "@alice:test"
        remote_key_user_id = "@fjqYXanwu0q4AvejqOQVMQ8pxwG3q3ZZQTOCD0ncR30:remote"
        remote_name_user_id = "@bob:remote"
        self.get_success(
            self.store.store_verified_account_name_user_ids(
                {
                    local_key_user_id: local_name_user_id,
                    remote_key_user_id: remote_name_user_id,
                },
                self.clock.time_msec(),
            )
        )
        result = self.get_success(
            self.store.get_account_name_user_ids_for_account_key_user_ids(
                [local_key_user_id, remote_key_user_id]
            ),
        )
        self.assertEquals(result[local_key_user_id], local_name_user_id)
        self.assertEquals(result[remote_key_user_id], remote_name_user_id)

    def test_get_unverified_account_key_user_ids(self) -> None:
        user1 = "@fjqYXanwu0q4AvejqOQVMQ8pxwG3q3ZZQTOCD0ncR30:remote"
        user2 = "@6fey6W1wS3-vbvUmHZnTd6Gi3o-TIxvIcwtEQP4nrW0:remote"
        user3 = "@9GgRdarGTiGMNxBoVf3C8d00UUF9kpOO0JdWsoyZ6eY:somewhere"
        self.get_success(
            self.store.store_unverified_account_key_user_ids([user1, user2, user3])
        )
        got_user_ids = self.get_success(
            self.store.get_unverified_account_key_user_ids("remote")
        )
        got_user_ids.sort()
        self.assertEquals(got_user_ids, [user2, user1])

    def test_unverified_to_verified_account(self) -> None:
        user1 = "@fjqYXanwu0q4AvejqOQVMQ8pxwG3q3ZZQTOCD0ncR30:remote"
        user1_name = "@alice:remote"
        user2 = "@6fey6W1wS3-vbvUmHZnTd6Gi3o-TIxvIcwtEQP4nrW0:remote"
        # Store both users as unverified
        self.get_success(
            self.store.store_unverified_account_key_user_ids([user1, user2])
        )
        # Verify user 1
        self.get_success(
            self.store.store_verified_account_name_user_ids(
                {
                    user1: user1_name,
                },
                self.clock.time_msec(),
            ),
        )
        # User 1 must not be unverified
        got_user_ids = self.get_success(
            self.store.get_unverified_account_key_user_ids("remote")
        )
        self.assertEquals(got_user_ids, [user2])
        # User 1 must be verified
        key_to_name = self.get_success(
            self.store.get_account_name_user_ids_for_account_key_user_ids(
                [user1],
            )
        )
        self.assertEquals(
            key_to_name,
            {
                user1: user1_name,
            },
        )

    def test_verified_to_unverified_account_noops(self) -> None:
        user1 = "@fjqYXanwu0q4AvejqOQVMQ8pxwG3q3ZZQTOCD0ncR30:remote"
        user1_name = "@alice:remote"
        user2 = "@6fey6W1wS3-vbvUmHZnTd6Gi3o-TIxvIcwtEQP4nrW0:remote"
        # Verify user 1
        self.get_success(
            self.store.store_verified_account_name_user_ids(
                {
                    user1: user1_name,
                },
                self.clock.time_msec(),
            ),
        )
        # Store both users as unverified
        self.get_success(
            self.store.store_unverified_account_key_user_ids([user1, user2])
        )
        # User 1 must not be unverified
        got_user_ids = self.get_success(
            self.store.get_unverified_account_key_user_ids("remote")
        )
        self.assertEquals(got_user_ids, [user2])
        # User 1 must be verified
        key_to_name = self.get_success(
            self.store.get_account_name_user_ids_for_account_key_user_ids(
                [user1],
            )
        )
        self.assertEquals(
            key_to_name,
            {
                user1: user1_name,
            },
        )
