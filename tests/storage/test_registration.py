#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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
from twisted.internet.testing import MemoryReactor

from synapse.api.constants import UserTypes
from synapse.api.errors import ThreepidValidationError
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import PostgresEngine
from synapse.types import JsonDict, UserID, UserInfo
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, override_config


class RegistrationStoreTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.user_id = "@my-user:test"
        self.tokens = ["AbCdEfGhIjKlMnOpQrStUvWxYz", "BcDeFgHiJkLmNoPqRsTuVwXyZa"]
        self.pwhash = "{xx1}123456789"
        self.device_id = "akgjhdjklgshg"

    def test_register(self) -> None:
        self.get_success(self.store.register_user(self.user_id, self.pwhash))

        self.assertEqual(
            UserInfo(
                user_id=UserID.from_string(self.user_id),
                is_admin=False,
                is_guest=False,
                consent_server_notice_sent=None,
                consent_ts=None,
                consent_version=None,
                appservice_id=None,
                creation_ts=0,
                user_type=None,
                is_deactivated=False,
                locked=False,
                is_shadow_banned=False,
                approved=True,
                suspended=False,
            ),
            (self.get_success(self.store.get_user_by_id(self.user_id))),
        )

    def test_consent(self) -> None:
        self.get_success(self.store.register_user(self.user_id, self.pwhash))
        before_consent = self.clock.time_msec()
        self.reactor.advance(5)
        self.get_success(self.store.user_set_consent_version(self.user_id, "1"))
        self.reactor.advance(5)

        user = self.get_success(self.store.get_user_by_id(self.user_id))
        assert user
        self.assertEqual(user.consent_version, "1")
        self.assertIsNotNone(user.consent_ts)
        assert user.consent_ts is not None
        self.assertGreater(user.consent_ts, before_consent)
        self.assertLess(user.consent_ts, self.clock.time_msec())

    def test_add_tokens(self) -> None:
        self.get_success(self.store.register_user(self.user_id, self.pwhash))
        self.get_success(
            self.store.add_access_token_to_user(
                self.user_id, self.tokens[1], self.device_id, valid_until_ms=None
            )
        )

        result = self.get_success(self.store.get_user_by_access_token(self.tokens[1]))

        assert result
        self.assertEqual(result.user_id, self.user_id)
        self.assertEqual(result.device_id, self.device_id)
        self.assertIsNotNone(result.token_id)

    def test_user_delete_access_tokens(self) -> None:
        # add some tokens
        self.get_success(self.store.register_user(self.user_id, self.pwhash))
        self.get_success(
            self.store.add_access_token_to_user(
                self.user_id, self.tokens[0], device_id=None, valid_until_ms=None
            )
        )
        self.get_success(
            self.store.add_access_token_to_user(
                self.user_id, self.tokens[1], self.device_id, valid_until_ms=None
            )
        )

        # now delete some
        self.get_success(
            self.store.user_delete_access_tokens(self.user_id, device_id=self.device_id)
        )

        # check they were deleted
        user = self.get_success(self.store.get_user_by_access_token(self.tokens[1]))
        self.assertIsNone(user, "access token was not deleted by device_id")

        # check the one not associated with the device was not deleted
        user = self.get_success(self.store.get_user_by_access_token(self.tokens[0]))
        assert user
        self.assertEqual(self.user_id, user.user_id)

        # now delete the rest
        self.get_success(self.store.user_delete_access_tokens(self.user_id))

        user = self.get_success(self.store.get_user_by_access_token(self.tokens[0]))
        self.assertIsNone(user, "access token was not deleted without device_id")

    def test_is_support_user(self) -> None:
        TEST_USER = "@test:test"
        SUPPORT_USER = "@support:test"

        res = self.get_success(self.store.is_support_user(None))  # type: ignore[arg-type]
        self.assertFalse(res)
        self.get_success(
            self.store.register_user(user_id=TEST_USER, password_hash=None)
        )
        res = self.get_success(self.store.is_support_user(TEST_USER))
        self.assertFalse(res)

        self.get_success(
            self.store.register_user(
                user_id=SUPPORT_USER, password_hash=None, user_type=UserTypes.SUPPORT
            )
        )
        res = self.get_success(self.store.is_support_user(SUPPORT_USER))
        self.assertTrue(res)

    def test_get_users_by_id_case_insensitive(self) -> None:
        """Looking up a user ID returns every user whose ID only differs by case."""
        self.get_success(self.store.register_user("@Alice:test", "hash1"))
        self.get_success(self.store.register_user("@alice:test", "hash2"))
        self.get_success(self.store.register_user("@bob:test", "hash3"))

        res = self.get_success(
            self.store.get_users_by_id_case_insensitive("@ALICE:test")
        )
        self.assertEqual(res, {"@Alice:test": "hash1", "@alice:test": "hash2"})

        res = self.get_success(
            self.store.get_users_by_id_case_insensitive("@carol:test")
        )
        self.assertEqual(res, {})

    def test_users_lower_name_index(self) -> None:
        """The `users_lower_name` background update creates an index that serves
        case-insensitive user ID lookups.
        """
        self.get_success(
            self.store.db_pool.runInteraction(
                "drop_users_lower_name",
                lambda txn: txn.execute("DROP INDEX users_lower_name"),
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                table="background_updates",
                values={"update_name": "users_lower_name", "progress_json": "{}"},
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        def explain_txn(txn: LoggingTransaction) -> str:
            if isinstance(self.store.database_engine, PostgresEngine):
                txn.execute("SET LOCAL enable_seqscan = off")
                explain = "EXPLAIN"
            else:
                explain = "EXPLAIN QUERY PLAN"
            txn.execute(
                f"{explain} SELECT name, password_hash FROM users"
                " WHERE lower(name) = lower(?)",
                ("@Alice:test",),
            )
            return "\n".join(str(row) for row in txn.fetchall())

        plan = self.get_success(
            self.store.db_pool.runInteraction("explain_users_lower_name", explain_txn)
        )
        self.assertIn("users_lower_name", plan)

    @override_config({"request_token_inhibit_3pid_errors": False})
    def test_3pid_inhibit_invalid_validation_session_error(self) -> None:
        """Tests that enabling the configuration option to inhibit 3PID errors on
        /requestToken also inhibits validation errors caused by an unknown session ID.
        """

        # Check that, with the config setting explicitly set to false, a validation
        # error is caused by the unknown session ID.
        e = self.get_failure(
            self.store.validate_threepid_session(
                "fake_sid",
                "fake_client_secret",
                "fake_token",
                0,
            ),
            ThreepidValidationError,
        )
        self.assertEqual(e.value.msg, "Unknown session_id", e)

        # Set the config setting to true.
        self.store._ignore_unknown_session_error = True

        # Check that now the validation error is caused by the token not matching.
        e = self.get_failure(
            self.store.validate_threepid_session(
                "fake_sid",
                "fake_client_secret",
                "fake_token",
                0,
            ),
            ThreepidValidationError,
        )
        self.assertEqual(e.value.msg, "Validation token not found or has expired", e)


class ApprovalRequiredRegistrationTestCase(HomeserverTestCase):
    def default_config(self) -> JsonDict:
        config = super().default_config()

        # If there's already some config for this feature in the default config, it
        # means we're overriding it with @override_config. In this case we don't want
        # to do anything more with it.
        msc3866_config = config.get("experimental_features", {}).get("msc3866")
        if msc3866_config is not None:
            return config

        # Require approval for all new accounts.
        config["experimental_features"] = {
            "msc3866": {
                "enabled": True,
                "require_approval_for_new_accounts": True,
            }
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.user_id = "@my-user:test"
        self.pwhash = "{xx1}123456789"

    @override_config(
        {
            "experimental_features": {
                "msc3866": {
                    "enabled": True,
                    "require_approval_for_new_accounts": False,
                }
            }
        }
    )
    def test_approval_not_required(self) -> None:
        """Tests that if we don't require approval for new accounts, newly created
        accounts are automatically marked as approved.
        """
        self.get_success(self.store.register_user(self.user_id, self.pwhash))

        user = self.get_success(self.store.get_user_by_id(self.user_id))
        assert user is not None
        self.assertTrue(user.approved)

        approved = self.get_success(self.store.is_user_approved(self.user_id))
        self.assertTrue(approved)

    def test_approval_required(self) -> None:
        """Tests that if we require approval for new accounts, newly created accounts
        are not automatically marked as approved.
        """
        self.get_success(self.store.register_user(self.user_id, self.pwhash))

        user = self.get_success(self.store.get_user_by_id(self.user_id))
        assert user is not None
        self.assertFalse(user.approved)

        approved = self.get_success(self.store.is_user_approved(self.user_id))
        self.assertFalse(approved)

    def test_override(self) -> None:
        """Tests that if we require approval for new accounts, but we explicitly say the
        new user should be considered approved, they're marked as approved.
        """
        self.get_success(
            self.store.register_user(
                self.user_id,
                self.pwhash,
                approved=True,
            )
        )

        user = self.get_success(self.store.get_user_by_id(self.user_id))
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.approved, 1)

        approved = self.get_success(self.store.is_user_approved(self.user_id))
        self.assertTrue(approved)

    def test_approve_user(self) -> None:
        """Tests that approving the user updates their approval status."""
        self.get_success(self.store.register_user(self.user_id, self.pwhash))

        approved = self.get_success(self.store.is_user_approved(self.user_id))
        self.assertFalse(approved)

        self.get_success(
            self.store.update_user_approval_status(
                UserID.from_string(self.user_id), True
            )
        )

        approved = self.get_success(self.store.is_user_approved(self.user_id))
        self.assertTrue(approved)
