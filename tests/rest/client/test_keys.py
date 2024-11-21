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
import urllib.parse
from copy import deepcopy
from http import HTTPStatus
from unittest.mock import patch

from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.sign import sign_json

from synapse.api.errors import Codes
from synapse.rest import admin
from synapse.rest.client import keys, login
from synapse.types import JsonDict, Requester, create_requester

from tests import unittest
from tests.http.server._base import make_request_with_cancellation_test
from tests.unittest import override_config
from tests.utils import HAS_AUTHLIB


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


class UnsignedKeyDataTestCase(unittest.HomeserverTestCase):
    servlets = [
        keys.register_servlets,
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4229_enabled": True}
        return config

    def make_key_data(self, user_id: str, device_id: str) -> JsonDict:
        return {
            "algorithms": ["m.olm.v1.curve25519-aes-sha2", "m.megolm.v1.aes-sha2"],
            "device_id": device_id,
            "keys": {
                f"curve25519:{device_id}": "keykeykey",
                f"ed25519:{device_id}": "keykeykey",
            },
            "signatures": {user_id: {f"ed25519:{device_id}": "sigsigsig"}},
            "user_id": user_id,
        }

    def test_unsigned_uploaded_data_returned_in_keys_query(self) -> None:
        password = "wonderland"
        device_id = "ABCDEFGHI"
        alice_id = self.register_user("alice", password)
        alice_token = self.login(
            "alice",
            password,
            device_id=device_id,
            additional_request_fields={"initial_device_display_name": "mydevice"},
        )

        # Alice uploads some keys, with a bit of unsigned data
        keys1 = self.make_key_data(alice_id, device_id)
        keys1["unsigned"] = {"a": "b"}

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {"device_keys": keys1},
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

        # /keys/query should return the unsigned data, with the device display name merged in.
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/query",
            {"device_keys": {alice_id: []}},
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        device_response = channel.json_body["device_keys"][alice_id][device_id]
        expected_device_response = deepcopy(keys1)
        expected_device_response["unsigned"]["device_display_name"] = "mydevice"
        self.assertEqual(device_response, expected_device_response)

        # /_matrix/federation/v1/user/devices/{userId} should return the unsigned data too
        fed_response = self.get_success(
            self.hs.get_device_handler().on_federation_query_user_devices(alice_id)
        )
        self.assertEqual(
            fed_response["devices"][0],
            {"device_id": device_id, "keys": keys1},
        )

        # so should /_matrix/federation/v1/user/keys/query
        fed_response = self.get_success(
            self.hs.get_e2e_keys_handler().on_federation_query_client_keys(
                {"device_keys": {alice_id: []}}
            )
        )
        fed_device_response = fed_response["device_keys"][alice_id][device_id]
        self.assertEqual(fed_device_response, keys1)

    def test_non_dict_unsigned_is_ignored(self) -> None:
        password = "wonderland"
        device_id = "ABCDEFGHI"
        alice_id = self.register_user("alice", password)
        alice_token = self.login(
            "alice",
            password,
            device_id=device_id,
            additional_request_fields={"initial_device_display_name": "mydevice"},
        )

        # Alice uploads some keys, with a malformed unsigned data
        keys1 = self.make_key_data(alice_id, device_id)
        keys1["unsigned"] = ["a", "b"]  # a list!

        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/upload",
            {"device_keys": keys1},
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)

        # /keys/query should return the unsigned data, with the device display name merged in.
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/keys/query",
            {"device_keys": {alice_id: []}},
            alice_token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.result)
        device_response = channel.json_body["device_keys"][alice_id][device_id]
        expected_device_response = deepcopy(keys1)
        expected_device_response["unsigned"] = {"device_display_name": "mydevice"}
        self.assertEqual(device_response, expected_device_response)

        # /_matrix/federation/v1/user/devices/{userId} should return the unsigned data too
        fed_response = self.get_success(
            self.hs.get_device_handler().on_federation_query_user_devices(alice_id)
        )
        self.assertEqual(
            fed_response["devices"][0],
            {"device_id": device_id, "keys": keys1},
        )

        # so should /_matrix/federation/v1/user/keys/query
        fed_response = self.get_success(
            self.hs.get_e2e_keys_handler().on_federation_query_client_keys(
                {"device_keys": {alice_id: []}}
            )
        )
        fed_device_response = fed_response["device_keys"][alice_id][device_id]
        expected_device_response = deepcopy(keys1)
        expected_device_response["unsigned"] = {}
        self.assertEqual(fed_device_response, expected_device_response)


class SigningKeyUploadServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        keys.register_servlets,
    ]

    OIDC_ADMIN_TOKEN = "_oidc_admin_token"

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "enable_registration": False,
            "experimental_features": {
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "account_management_url": "https://my-account.issuer",
                    "client_id": "id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "secret",
                    "admin_token": OIDC_ADMIN_TOKEN,
                },
            },
        }
    )
    def test_master_cross_signing_key_replacement_msc3861(self) -> None:
        # Provision a user like MAS would, cribbing from
        # https://github.com/matrix-org/matrix-authentication-service/blob/08d46a79a4adb22819ac9d55e15f8375dfe2c5c7/crates/matrix-synapse/src/lib.rs#L224-L229
        alice = "@alice:test"
        channel = self.make_request(
            "PUT",
            f"/_synapse/admin/v2/users/{urllib.parse.quote(alice)}",
            access_token=self.OIDC_ADMIN_TOKEN,
            content={},
        )
        self.assertEqual(channel.code, HTTPStatus.CREATED, channel.json_body)

        # Provision a device like MAS would, cribbing from
        # https://github.com/matrix-org/matrix-authentication-service/blob/08d46a79a4adb22819ac9d55e15f8375dfe2c5c7/crates/matrix-synapse/src/lib.rs#L260-L262
        alice_device = "alice_device"
        channel = self.make_request(
            "POST",
            f"/_synapse/admin/v2/users/{urllib.parse.quote(alice)}/devices",
            access_token=self.OIDC_ADMIN_TOKEN,
            content={"device_id": alice_device},
        )
        self.assertEqual(channel.code, HTTPStatus.CREATED, channel.json_body)

        # Prepare a mock MAS access token.
        alice_token = "alice_token_1234_oidcwhatyoudidthere"

        async def mocked_get_user_by_access_token(
            token: str, allow_expired: bool = False
        ) -> Requester:
            self.assertEqual(token, alice_token)
            return create_requester(
                user_id=alice,
                device_id=alice_device,
                scope=[],
                is_guest=False,
            )

        patch_get_user_by_access_token = patch.object(
            self.hs.get_auth(),
            "get_user_by_access_token",
            wraps=mocked_get_user_by_access_token,
        )

        # Copied from E2eKeysHandlerTestCase
        master_pubkey = "nqOvzeuGWT/sRx3h7+MHoInYj3Uk2LD/unI9kDYcHwk"
        master_pubkey2 = "fHZ3NPiKxoLQm5OoZbKa99SYxprOjNs4TwJUKP+twCM"
        master_pubkey3 = "85T7JXPFBAySB/jwby4S3lBPTqY3+Zg53nYuGmu1ggY"

        master_key: JsonDict = {
            "user_id": alice,
            "usage": ["master"],
            "keys": {"ed25519:" + master_pubkey: master_pubkey},
        }
        master_key2: JsonDict = {
            "user_id": alice,
            "usage": ["master"],
            "keys": {"ed25519:" + master_pubkey2: master_pubkey2},
        }
        master_key3: JsonDict = {
            "user_id": alice,
            "usage": ["master"],
            "keys": {"ed25519:" + master_pubkey3: master_pubkey3},
        }

        with patch_get_user_by_access_token:
            # Upload an initial cross-signing key.
            channel = self.make_request(
                "POST",
                "/_matrix/client/v3/keys/device_signing/upload",
                access_token=alice_token,
                content={
                    "master_key": master_key,
                },
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

            # Should not be able to upload another master key.
            channel = self.make_request(
                "POST",
                "/_matrix/client/v3/keys/device_signing/upload",
                access_token=alice_token,
                content={
                    "master_key": master_key2,
                },
            )
            self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.json_body)

        # Pretend that MAS did UIA and allowed us to replace the master key.
        channel = self.make_request(
            "POST",
            f"/_synapse/admin/v1/users/{urllib.parse.quote(alice)}/_allow_cross_signing_replacement_without_uia",
            access_token=self.OIDC_ADMIN_TOKEN,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, msg=channel.json_body)

        with patch_get_user_by_access_token:
            # Should now be able to upload master key2.
            channel = self.make_request(
                "POST",
                "/_matrix/client/v3/keys/device_signing/upload",
                access_token=alice_token,
                content={
                    "master_key": master_key2,
                },
            )
            self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

            # Even though we're still in the grace period, we shouldn't be able to
            # upload master key 3 immediately after uploading key 2.
            channel = self.make_request(
                "POST",
                "/_matrix/client/v3/keys/device_signing/upload",
                access_token=alice_token,
                content={
                    "master_key": master_key3,
                },
            )
            self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.json_body)

        # Pretend that MAS did UIA and allowed us to replace the master key.
        channel = self.make_request(
            "POST",
            f"/_synapse/admin/v1/users/{urllib.parse.quote(alice)}/_allow_cross_signing_replacement_without_uia",
            access_token=self.OIDC_ADMIN_TOKEN,
        )
        self.assertEqual(HTTPStatus.OK, channel.code, msg=channel.json_body)
        timestamp_ms = channel.json_body["updatable_without_uia_before_ms"]

        # Advance to 1 second after the replacement period ends.
        self.reactor.advance(timestamp_ms - self.clock.time_msec() + 1000)

        with patch_get_user_by_access_token:
            # We should not be able to upload master key3 because the replacement has
            # expired.
            channel = self.make_request(
                "POST",
                "/_matrix/client/v3/keys/device_signing/upload",
                access_token=alice_token,
                content={
                    "master_key": master_key3,
                },
            )
            self.assertEqual(channel.code, HTTPStatus.UNAUTHORIZED, channel.json_body)
