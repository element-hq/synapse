#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from typing import Iterable, Optional, Set

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import AccountDataTypes
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util import Clock

from tests import unittest


class IgnoredUsersTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.user = "@user:test"

    def _update_ignore_list(
        self, *ignored_user_ids: Iterable[str], ignorer_user_id: Optional[str] = None
    ) -> None:
        """Update the account data to block the given users."""
        if ignorer_user_id is None:
            ignorer_user_id = self.user

        self.get_success(
            self.store.add_account_data_for_user(
                ignorer_user_id,
                AccountDataTypes.IGNORED_USER_LIST,
                {"ignored_users": {u: {} for u in ignored_user_ids}},
            )
        )

    def assert_ignorers(
        self, ignored_user_id: str, expected_ignorer_user_ids: Set[str]
    ) -> None:
        self.assertEqual(
            self.get_success(self.store.ignored_by(ignored_user_id)),
            expected_ignorer_user_ids,
        )

    def assert_ignored(
        self, ignorer_user_id: str, expected_ignored_user_ids: Set[str]
    ) -> None:
        self.assertEqual(
            self.get_success(self.store.ignored_users(ignorer_user_id)),
            expected_ignored_user_ids,
        )

    def test_ignoring_users(self) -> None:
        """Basic adding/removing of users from the ignore list."""
        self._update_ignore_list("@other:test", "@another:remote")
        self.assert_ignored(self.user, {"@other:test", "@another:remote"})

        # Check a user which no one ignores.
        self.assert_ignorers("@user:test", set())

        # Check a local user which is ignored.
        self.assert_ignorers("@other:test", {self.user})

        # Check a remote user which is ignored.
        self.assert_ignorers("@another:remote", {self.user})

        # Add one user, remove one user, and leave one user.
        self._update_ignore_list("@foo:test", "@another:remote")
        self.assert_ignored(self.user, {"@foo:test", "@another:remote"})

        # Check the removed user.
        self.assert_ignorers("@other:test", set())

        # Check the added user.
        self.assert_ignorers("@foo:test", {self.user})

        # Check the removed user.
        self.assert_ignorers("@another:remote", {self.user})

    def test_caching(self) -> None:
        """Ensure that caching works properly between different users."""
        # The first user ignores a user.
        self._update_ignore_list("@other:test")
        self.assert_ignored(self.user, {"@other:test"})
        self.assert_ignorers("@other:test", {self.user})

        # The second user ignores them.
        self._update_ignore_list("@other:test", ignorer_user_id="@second:test")
        self.assert_ignored("@second:test", {"@other:test"})
        self.assert_ignorers("@other:test", {self.user, "@second:test"})

        # The first user un-ignores them.
        self._update_ignore_list()
        self.assert_ignored(self.user, set())
        self.assert_ignorers("@other:test", {"@second:test"})

    def test_invalid_data(self) -> None:
        """Invalid data ends up clearing out the ignored users list."""
        # Add some data and ensure it is there.
        self._update_ignore_list("@other:test")
        self.assert_ignored(self.user, {"@other:test"})
        self.assert_ignorers("@other:test", {self.user})

        # No ignored_users key.
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.IGNORED_USER_LIST,
                {},
            )
        )

        # No one ignores the user now.
        self.assert_ignored(self.user, set())
        self.assert_ignorers("@other:test", set())

        # Add some data and ensure it is there.
        self._update_ignore_list("@other:test")
        self.assert_ignored(self.user, {"@other:test"})
        self.assert_ignorers("@other:test", {self.user})

        # Invalid data.
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.IGNORED_USER_LIST,
                {"ignored_users": "unexpected"},
            )
        )

        # No one ignores the user now.
        self.assert_ignored(self.user, set())
        self.assert_ignorers("@other:test", set())

    def test_ignoring_users_with_latest_stream_ids(self) -> None:
        """Test that ignoring users updates the latest stream ID for the ignored
        user list account data."""

        def get_latest_ignore_streampos(user_id: str) -> Optional[int]:
            return self.get_success(
                self.store.get_latest_stream_id_for_global_account_data_by_type_for_user(
                    user_id, AccountDataTypes.IGNORED_USER_LIST
                )
            )

        self.assertIsNone(get_latest_ignore_streampos("@user:test"))

        self._update_ignore_list("@other:test", "@another:remote")

        self.assertEqual(get_latest_ignore_streampos("@user:test"), 2)

        # Add one user, remove one user, and leave one user.
        self._update_ignore_list("@foo:test", "@another:remote")

        self.assertEqual(get_latest_ignore_streampos("@user:test"), 3)


class InviteFilterTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = self.hs.get_datastores().main
        self.user = "@user:test"

    def test_unset(self) -> None:
        """Check that a user who has never set a config accepts invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertTrue(config.invite_allowed(UserID.from_string("@other:test")))

    def test_empty(self) -> None:
        """Check that a user who has set their account data to empty object (deleted) accepts invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertTrue(config.invite_allowed(UserID.from_string("@other:test")))

    def test_default_allow(self) -> None:
        """Check that a user who has set their account data to allow all invites accepts invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "allow"},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertTrue(config.invite_allowed(UserID.from_string("@other:test")))

    def test_default_block(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "block"},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertFalse(config.invite_allowed(UserID.from_string("@other:test")))

    def test_block_user(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "allow", "user_exceptions": {"@baduser:test": "block"}},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))

        self.assertFalse(config.invite_allowed(UserID.from_string("@baduser:test")))
        self.assertTrue(config.invite_allowed(UserID.from_string("@gooduser:test")))

    def test_allow_user(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "block", "user_exceptions": {"@gooduser:test": "allow"}},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))

        self.assertFalse(config.invite_allowed(UserID.from_string("@baduser:test")))
        self.assertTrue(config.invite_allowed(UserID.from_string("@gooduser:test")))

    @unittest.override_config(
        {"server_notices": {"system_mxid_localpart": "notice_user"}}
    )
    def test_always_allow_server_notices_user(self) -> None:
        """Check that users cannot block the server notices user."""

        # Either because of the defaults
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "block"},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertTrue(config.invite_allowed(UserID.from_string("@notice_user:test")))

        # Or specific exceptions
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "allow", "user_exceptions": {"@notice_user:test": "block"}},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertTrue(config.invite_allowed(UserID.from_string("@notice_user:test")))

        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "allow", "server_exceptions": {"test": "block"}},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))
        self.assertTrue(config.invite_allowed(UserID.from_string("@notice_user:test")))

    def test_block_server(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "allow", "server_exceptions": {"badserver.org": "block"}},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))

        self.assertFalse(
            config.invite_allowed(UserID.from_string("@baduser:badserver.org"))
        )
        self.assertTrue(config.invite_allowed(UserID.from_string("@gooduser:test")))

    def test_allow_server(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {"default": "block", "server_exceptions": {"goodserver.org": "allow"}},
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))

        self.assertFalse(
            config.invite_allowed(UserID.from_string("@baduser:badserver.org"))
        )
        self.assertTrue(
            config.invite_allowed(UserID.from_string("@gooduser:goodserver.org"))
        )

    def test_mixed_rules_block(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {
                    "default": "block",
                    "server_exceptions": {"goodserver.org": "allow"},
                    "user_exceptions": {
                        "@baduser:goodserver.org": "block",
                    },
                },
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))

        self.assertFalse(
            config.invite_allowed(UserID.from_string("@baduser:goodserver.org"))
        )
        self.assertFalse(
            config.invite_allowed(UserID.from_string("@other:unlisted.org"))
        )
        self.assertTrue(
            config.invite_allowed(UserID.from_string("@gooduser:goodserver.org"))
        )

    def test_mixed_rules_allow(self) -> None:
        """Check that a user who has set their account data to block all invites blocks invites."""
        self.get_success(
            self.store.add_account_data_for_user(
                self.user,
                AccountDataTypes.INVITE_PERMISSION_CONFIG,
                {
                    "default": "block",
                    "server_exceptions": {"badserver.org": "block"},
                    "user_exceptions": {
                        "@gooduser:badserver.org": "allow",
                    },
                },
            )
        )
        config = self.get_success(self.store.get_invite_config_for_user(self.user))

        self.assertFalse(
            config.invite_allowed(UserID.from_string("@anyuser:badserver.org"))
        )
        self.assertFalse(
            config.invite_allowed(UserID.from_string("@other:unlisted.org"))
        )
        self.assertTrue(
            config.invite_allowed(UserID.from_string("@gooduser:badserver.org"))
        )
