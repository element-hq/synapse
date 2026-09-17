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

from signedjson.key import (
    encode_verify_key_base64,
    generate_signing_key,
    get_verify_key,
)
from signedjson.sign import sign_json

from synapse.api.errors import Codes
from synapse.rest import admin
from synapse.rest.client import keys, login
from synapse.types import JsonDict

from tests import unittest
from tests.http.server._base import make_request_with_cancellation_test


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


class SigningKeyUploadServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        keys.register_servlets,
    ]

    OIDC_ADMIN_TOKEN = "_oidc_admin_token"
    ACCOUNT_MANAGEMENT_URL = "https://my-account.issuer"
