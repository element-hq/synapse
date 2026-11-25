#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2021 The Matrix.org Foundation C.I.C.
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
from http import HTTPStatus
from unittest.mock import AsyncMock

from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.sign import sign_json
from signedjson.types import SigningKey

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import Codes
from synapse.rest import admin
from synapse.rest.client import keys, login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util.clock import Clock

from tests import unittest
from tests.http.server._base import make_request_with_cancellation_test
from tests.server import FakeChannel


class KeyUploadTestCase(unittest.HomeserverTestCase):
    servlets = [
        keys.register_servlets,
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
    ]

    def test_upload_keys_fails_on_invalid_structure(self) -> None:
        """Check that we validate the structure of keys upon upload.

        Regression test for https://github.com/element-hq/synapse/pull/17097
        """
        self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland")

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                # Error: device_keys must be a dict
                "device_keys": ["some", "stuff", "weewoo"]
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                # Error: properties of fallback_keys must be in the form `<algorithm>:<device_id>`
                "fallback_keys": {"invalid_key": "signature_base64"}
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                # Same as above, but for one_time_keys
                "one_time_keys": {"invalid_key": "signature_base64"}
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

    def test_upload_keys_fails_on_invalid_user_id_or_device_id(self) -> None:
        """
        Validate that the requesting user is uploading their own keys and nobody
        else's.
        """
        device_id = "DEVICE_ID"
        alice_user_id = self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland", device_id=device_id)

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                "device_keys": {
                    # Included `user_id` does not match requesting user.
                    "user_id": "@unknown_user:test",
                    "device_id": device_id,
                    "algorithms": ["m.olm.curve25519-aes-sha2"],
                    "keys": {
                        f"ed25519:{device_id}": "publickey",
                    },
                    "signatures": {},
                }
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                "device_keys": {
                    "user_id": alice_user_id,
                    # Included `device_id` does not match requesting user's.
                    "device_id": "UNKNOWN_DEVICE_ID",
                    "algorithms": ["m.olm.curve25519-aes-sha2"],
                    "keys": {
                        f"ed25519:{device_id}": "publickey",
                    },
                    "signatures": {},
                }
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

    def test_upload_keys_rejects_device_keys_set_to_null(self) -> None:
        """
        Test that sending `device_keys: null` is rejected per spec.
        The spec says `device_keys` may be omitted, but not set to `null`.

        See https://github.com/element-hq/synapse/issues/19030.
        """
        device_id = "DEVICE_ID"
        self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland", device_id=device_id)

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {
                "device_keys": None,
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)


class KeyQueryTestCase(unittest.HomeserverTestCase):
    servlets = [
        keys.register_servlets,
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
    ]

    def test_rejects_device_id_ice_key_outside_of_list(self) -> None:
        self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland")
        bob = self.register_user("bob", "uncle")
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    bob: "device_id1",
                },
            },
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

    def test_rejects_device_key_given_as_map_to_bool(self) -> None:
        self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland")
        bob = self.register_user("bob", "uncle")
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    bob: {
                        "device_id1": True,
                    },
                },
            },
            alice_token,
        )

        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

    def test_requires_device_key(self) -> None:
        """`device_keys` is required. We should complain if it's missing."""
        self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland")
        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/keys/query",
            {},
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(
            channel.json_body["errcode"],
            Codes.BAD_JSON,
            channel.result,
        )

    def test_key_query_cancellation(self) -> None:
        """
        Tests that /keys/query is cancellable and does not swallow the
        CancelledError.
        """
        self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland")

        bob = self.register_user("bob", "uncle")

        channel = make_request_with_cancellation_test(
            "test_key_query_cancellation",
            self.reactor,
            self.site,
            "POST",
            "/_matrix/client/r0/keys/query",
            {
                "device_keys": {
                    # Empty list means we request keys for all bob's devices
                    bob: [],
                },
            },
            token=alice_token,
        )

        self.assertEqual(200, channel.code, msg=channel.result["body"])
        self.assertIn(bob, channel.json_body["device_keys"])

    def make_device_keys(self, user_id: str, device_id: str) -> JsonDict:
        # We only generate a master key to simplify the test.
        master_signing_key = generate_signing_key(device_id)
        master_verify_key = encode_verify_key_base64(get_verify_key(master_signing_key))

        return {
            "master_key": sign_json(
                {
                    "user_id": user_id,
                    "usage": ["master"],
                    "keys": {"ed25519:" + master_verify_key: master_verify_key},
                },
                user_id,
                master_signing_key,
            ),
        }

    def test_device_signing_with_uia(self) -> None:
        password = "wonderland"
        device_id = "ABCDEFGHI"
        alice_id = self.register_user("alice", password)
        alice_token = self.login("alice", password, device_id=device_id)

        keys1 = self.make_device_keys(alice_id, device_id)

        # Initial request should succeed as no existing keys are present.
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys1,
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self._set_up_recovery(alice_id)

        keys2 = self.make_device_keys(alice_id, device_id)

        # Subsequent request should require UIA as keys already exist even though session_timeout is set.
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys2,
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.result)

        # Grab the session
        session = channel.json_body["session"]
        # Ensure that flows are what is expected.
        self.assertIn({"stages": ["m.login.password"]}, channel.json_body["flows"])

        # add UI auth
        keys2["auth"] = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": alice_id},
            "password": password,
            "session": session,
        }

        # Request should complete
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys2,
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_device_signing_null_or_non_object_auth(self) -> None:
        """A null `auth` asks for UIA like an omitted one, and a non-object `auth`
        is rejected, rather than either causing a 500."""
        password = "wonderland"
        device_id = "ABCDEFGHI"
        alice_id = self.register_user("alice", password)
        alice_token = self.login("alice", password, device_id=device_id)

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            self.make_device_keys(alice_id, device_id),
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self._set_up_recovery(alice_id)

        keys = self.make_device_keys(alice_id, device_id)
        keys["auth"] = None
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys,
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.result)
        self.assertIn("session", channel.json_body)

        keys["auth"] = "m.login.password"
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            keys,
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.BAD_REQUEST, channel.result)
        self.assertEqual(channel.json_body["errcode"], Codes.BAD_JSON)

    def _set_up_recovery(self, user_id: str) -> None:
        """Gives the user a way to verify, so that replacing their keys needs UIA."""
        account_data_handler = self.hs.get_account_data_handler()
        self.get_success(
            account_data_handler.add_account_data_for_user(
                user_id, "m.secret_storage.default_key", {"key": "recovery_key_id"}
            )
        )
        self.get_success(
            account_data_handler.add_account_data_for_user(
                user_id,
                "m.cross_signing.master",
                {"encrypted": {"recovery_key_id": {"ciphertext": "x"}}},
            )
        )


MAS_CONFIG: JsonDict = {
    "enabled": True,
    "endpoint": "http://localhost:8080/",
    "secret": "shared_secret",
}


class SigningKeyUploadServletTestCase(unittest.HomeserverTestCase):
    """Tests replacing cross-signing keys with MAS, where it normally needs approval in MAS."""

    servlets = [
        admin.register_servlets,
        keys.register_servlets,
    ]

    WITH_MAS = True

    OIDC_ADMIN_TOKEN = "_oidc_admin_token"
    ACCOUNT_MANAGEMENT_URL = "https://my-account.issuer"

    USER_ID = "@alice:test"
    DEVICE_ID = "ALICE1"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        if self.WITH_MAS:
            config["enable_registration"] = False
            config["matrix_authentication_service"] = MAS_CONFIG
        return config

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.store = homeserver.get_datastores().main
        self.e2e_keys_handler = homeserver.get_e2e_keys_handler()
        self.device_handler = homeserver.get_device_handler()
        self.account_data_handler = homeserver.get_account_data_handler()
        self.access_token = self._sign_in()

    def _sign_in(self) -> str:
        """Registers the user on `DEVICE_ID` and returns their access token."""
        self.get_success(self.store.register_user(self.USER_ID))
        self.get_success(
            self.device_handler.check_device_registered(self.USER_ID, self.DEVICE_ID)
        )

        # MAS isn't running, so fake what Synapse asks it
        auth = self.hs.get_auth()
        auth.get_user_by_req = AsyncMock(  # type: ignore[method-assign]
            return_value=create_requester(self.USER_ID, device_id=self.DEVICE_ID)
        )
        auth.account_management_url = AsyncMock(  # type: ignore[attr-defined]
            return_value=self.ACCOUNT_MANAGEMENT_URL
        )
        return "token"

    def test_reset_without_verification_methods(self) -> None:
        """Without a verified device or recovery, the keys can be replaced without re-authentication."""
        self._upload_cross_signing_keys()
        self._add_device("ALICE2")

        channel = self._reset()

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_reset_with_verified_device(self) -> None:
        """A device signed by the current identity means the reset needs re-authentication."""
        self_signing_key = self._upload_cross_signing_keys()
        self._add_device("ALICE2", self_signing_key)

        channel = self._reset()

        self._assert_needs_reauthentication(channel)

    def test_reset_from_verified_device(self) -> None:
        """The device asking for the reset counts as a way to verify too."""
        self_signing_key = self._upload_cross_signing_keys()
        self._add_device(self.DEVICE_ID, self_signing_key)

        channel = self._reset()

        self._assert_needs_reauthentication(channel)

    def test_reset_with_deleted_verified_device(self) -> None:
        """A verified device that was signed out doesn't count."""
        self_signing_key = self._upload_cross_signing_keys()
        self._add_device("ALICE2", self_signing_key)
        self.get_success(self.device_handler.delete_devices(self.USER_ID, ["ALICE2"]))

        channel = self._reset()

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_reset_with_device_verified_by_previous_identity(self) -> None:
        """A device signed by a previous identity doesn't count."""
        old_self_signing_key = self._upload_cross_signing_keys()
        self._add_device("ALICE2", old_self_signing_key)
        self.get_success(
            self.store.allow_master_cross_signing_key_replacement_without_uia(
                self.USER_ID, 60_000
            )
        )
        self._upload_cross_signing_keys()

        channel = self._reset()

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_reset_with_recovery(self) -> None:
        """The master key in secret storage means the reset needs re-authentication."""
        self._upload_cross_signing_keys()
        self._set_up_recovery()

        channel = self._reset()

        self._assert_needs_reauthentication(channel)

    def test_reset_after_default_key_wiped(self) -> None:
        """Wiping the default key before the reset, as the Rust SDK does, still needs re-authentication."""
        self._upload_cross_signing_keys()
        self._set_up_recovery()
        self._set_account_data("m.secret_storage.default_key", {})

        channel = self._reset()

        self._assert_needs_reauthentication(channel)

    def test_reset_after_master_key_secret_wiped(self) -> None:
        """Wiping the stored master key before the reset still needs re-authentication."""
        self._upload_cross_signing_keys()
        self._set_up_recovery()
        self._set_account_data("m.cross_signing.master", {"encrypted": {}})

        channel = self._reset()

        self._assert_needs_reauthentication(channel)

    def test_reset_after_default_key_deleted(self) -> None:
        """Deleting the default key, rather than replacing it, still needs re-authentication."""
        self._upload_cross_signing_keys()
        self._set_up_recovery()
        self.get_success(
            self.account_data_handler.remove_account_data_for_user(
                self.USER_ID, "m.secret_storage.default_key"
            )
        )

        channel = self._reset()

        self._assert_needs_reauthentication(channel)

    def test_reset_after_previous_identity_was_in_recovery(self) -> None:
        """Recovery of a previous identity doesn't count."""
        self._upload_cross_signing_keys()
        self._set_up_recovery()
        self._set_account_data("m.secret_storage.default_key", {})
        self.get_success(
            self.store.allow_master_cross_signing_key_replacement_without_uia(
                self.USER_ID, 60_000
            )
        )
        self._upload_cross_signing_keys()

        channel = self._reset()

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_reset_with_empty_secret_storage(self) -> None:
        """Empty secret storage account data doesn't count."""
        self._upload_cross_signing_keys()
        self._set_secret_storage(default_key={}, master_key_secret={"encrypted": {}})

        channel = self._reset()

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def test_reset_with_master_key_stored_with_other_key(self) -> None:
        """A master key stored with a key other than the default one doesn't count."""
        self._upload_cross_signing_keys()
        self._set_secret_storage(
            default_key={"key": "recovery_key_id"},
            master_key_secret={"encrypted": {"old_key_id": {"ciphertext": "x"}}},
        )

        channel = self._reset()

        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

    def _upload_cross_signing_keys(self) -> SigningKey:
        """Sets up a new identity and returns its self-signing key."""
        master_key = self._generate_cross_signing_key()
        self_signing_key = self._generate_cross_signing_key()

        self.get_success(
            self.e2e_keys_handler.upload_signing_keys_for_user(
                self.USER_ID,
                {
                    "master_key": self._make_cross_signing_key(master_key, "master"),
                    "self_signing_key": sign_json(
                        self._make_cross_signing_key(self_signing_key, "self_signing"),
                        self.USER_ID,
                        master_key,
                    ),
                },
            )
        )
        return self_signing_key

    def _add_device(
        self, device_id: str, self_signing_key: SigningKey | None = None
    ) -> None:
        """Adds a device with keys, signed by the self-signing key if given."""
        self.get_success(
            self.device_handler.check_device_registered(self.USER_ID, device_id)
        )
        device_key = generate_signing_key(device_id)
        device_keys = sign_json(
            {
                "user_id": self.USER_ID,
                "device_id": device_id,
                "algorithms": ["m.olm.v1.curve25519-aes-sha2"],
                "keys": {
                    f"ed25519:{device_id}": encode_verify_key_base64(
                        get_verify_key(device_key)
                    )
                },
            },
            self.USER_ID,
            device_key,
        )
        self.get_success(
            self.e2e_keys_handler.upload_keys_for_user(
                self.USER_ID, device_id, {"device_keys": device_keys}
            )
        )

        if self_signing_key is None:
            return

        result = self.get_success(
            self.e2e_keys_handler.upload_signatures_for_device_keys(
                self.USER_ID,
                {
                    self.USER_ID: {
                        device_id: sign_json(
                            device_keys, self.USER_ID, self_signing_key
                        )
                    }
                },
            )
        )
        self.assertEqual(result["failures"], {})

    def _set_secret_storage(
        self, default_key: JsonDict, master_key_secret: JsonDict
    ) -> None:
        self._set_account_data("m.secret_storage.default_key", default_key)
        self._set_account_data("m.cross_signing.master", master_key_secret)

    def _set_up_recovery(self) -> None:
        self._set_secret_storage(
            default_key={"key": "recovery_key_id"},
            master_key_secret={"encrypted": {"recovery_key_id": {"ciphertext": "x"}}},
        )

    def _set_account_data(self, account_data_type: str, content: JsonDict) -> None:
        # Through the handler, like client requests, which remembers recovery
        self.get_success(
            self.account_data_handler.add_account_data_for_user(
                self.USER_ID, account_data_type, content
            )
        )

    def _reset(self) -> FakeChannel:
        """Tries to replace the identity with a new master key."""
        return self.make_request(
            "POST",
            "/_matrix/client/v3/keys/device_signing/upload",
            {
                "master_key": self._make_cross_signing_key(
                    self._generate_cross_signing_key(), "master"
                )
            },
            access_token=self.access_token,
        )

    def _generate_cross_signing_key(self) -> SigningKey:
        # Cross-signing keys are identified by their public key.
        signing_key = generate_signing_key("")
        signing_key.version = encode_verify_key_base64(get_verify_key(signing_key))
        return signing_key

    def _make_cross_signing_key(self, signing_key: SigningKey, usage: str) -> JsonDict:
        public_key = signing_key.version
        return {
            "user_id": self.USER_ID,
            "usage": [usage],
            "keys": {f"ed25519:{public_key}": public_key},
        }

    def _assert_needs_reauthentication(self, channel: FakeChannel) -> None:
        """Checks that the user is sent to MAS to approve the reset."""
        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.result)
        self.assertIn({"stages": ["m.oauth"]}, channel.json_body["flows"])
        self.assertEqual(
            channel.json_body["params"]["m.oauth"]["url"],
            f"{self.ACCOUNT_MANAGEMENT_URL}?action=org.matrix.cross_signing_reset",
        )


class SigningKeyUploadServletWithoutMasTestCase(SigningKeyUploadServletTestCase):
    """The same tests without MAS, where replacing the keys normally needs UIA."""

    servlets = [
        admin.register_servlets,
        keys.register_servlets,
        login.register_servlets,
    ]

    WITH_MAS = False

    def _sign_in(self) -> str:
        self.register_user("alice", "password")
        return self.login("alice", "password", device_id=self.DEVICE_ID)

    def _assert_needs_reauthentication(self, channel: FakeChannel) -> None:
        """Checks that the user is asked for their password."""
        self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.result)
        self.assertIn({"stages": ["m.login.password"]}, channel.json_body["flows"])


class KeyChangesTestCase(unittest.HomeserverTestCase):
    servlets = [
        keys.register_servlets,
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def test_user_joins_after_invite(self) -> None:
        alice_id = self.register_user("alice", "wonderland")
        alice_token = self.login("alice", "wonderland")
        bob_id = self.register_user("bob", "builder")
        bob_token = self.login("bob", "builder")

        room_id = self.helper.create_room_as(alice_id, tok=alice_token)
        self.helper.invite(room_id, src=alice_id, targ=bob_id, tok=alice_token)

        channel = self.make_request("GET", "/sync", access_token=alice_token)
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        next_batch = channel.json_body["next_batch"]

        self.helper.join(room_id, bob_id, tok=bob_token)

        channel = self.make_request(
            "GET",
            f"/_matrix/client/v3/keys/changes?from={next_batch}&to={next_batch}",
            access_token=alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        self.assertEqual(channel.json_body["changed"], [bob_id])
        self.assertEqual(channel.json_body["left"], [])
