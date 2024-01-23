#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 Awesome Technologies Innovationslabor GmbH
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


from synapse.types import UserID

from tests import unittest


class DataStoreTestCase(unittest.HomeserverTestCase):
    def setUp(self) -> None:
        super().setUp()

        self.store = self.hs.get_datastores().main

        self.user = UserID.from_string("@abcde:test")
        self.displayname = "Frank"

    def test_get_users_paginate(self) -> None:
        self.get_success(self.store.register_user(self.user.to_string(), "pass"))
        self.get_success(self.store.create_profile(self.user))
        self.get_success(
            self.store.set_profile_displayname(self.user, self.displayname)
        )

        users, total = self.get_success(
            self.store.get_users_paginate(0, 10, name="bc", guests=False)
        )

        self.assertEqual(1, total)
        self.assertEqual(self.displayname, users.pop().displayname)

        users, total = self.get_success(
            self.store.get_users_paginate(0, 10, name="BC", guests=False)
        )

        self.assertEqual(1, total)
        self.assertEqual(self.displayname, users.pop().displayname)
