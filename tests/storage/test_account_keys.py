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
from synapse.util import Clock

from tests import unittest


class AccountKeysTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.user = "@user:test"

    def test_get_or_create_account_key_user_id_for_account_name_user_id(self) -> None:
        key_user_id, key = self.get_success(
            self.store.get_or_create_account_key_user_id_for_account_name_user_id(
                self.user
            )
        )
        # asserts the localpart is unpadded urlsafe base64
        self.assertRegex(key_user_id, r"^@[A-Za-z0-9\-_]{43}:test$")
        # asserts the key ID is the localpart
        self.assertEquals(key.version, get_localpart_from_id(key_user_id))
        # asserts the key ID is the public key
        self.assertEquals(
            key.version, encode_base64(get_verify_key(key).encode(), urlsafe=True)
        )
        # assert that repeated calls return the same key
        key_user_id2, key2 = self.get_success(
            self.store.get_or_create_account_key_user_id_for_account_name_user_id(
                self.user
            )
        )
        self.assertEquals(key_user_id, key_user_id2)
        self.assertEquals(key.encode(), key2.encode())

    def test_get_account_name_user_ids_for_account_key_user_ids(self) -> None:
        key_user_id, key = self.get_success(
            self.store.get_or_create_account_key_user_id_for_account_name_user_id(
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
            self.store.get_or_create_account_key_user_id_for_account_name_user_id(
                "@alice:test",
            )
        )
        key_user_id_bob, _ = self.get_success(
            self.store.get_or_create_account_key_user_id_for_account_name_user_id(
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
