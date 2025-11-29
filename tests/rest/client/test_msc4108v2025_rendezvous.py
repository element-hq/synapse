#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 Element Creations, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.


import json
import urllib.parse
from typing import Any, Mapping
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, rendezvous
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import checked_cast, override_config
from tests.utils import HAS_AUTHLIB

msc4108_endpoint = "/_matrix/client/unstable/io.element.msc4108/rendezvous"


class RendezvousServletTestCase(unittest.HomeserverTestCase):
    """
    Test the experimental MSC4108 rendezvous endpoint with the latest behaviour.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        rendezvous.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()
        return self.hs

    def setup_mock_oauth(self) -> None:
        """
        This isn't a very elegant way to mock the OAuth API, but it works for our purposes.
        """

        # Import this here so that we've checked that authlib is available.
        from synapse.api.auth.mas import MasDelegatedAuth

        self.auth = checked_cast(MasDelegatedAuth, self.hs.get_auth())

        self._rust_client = Mock(spec=["post"])
        self._rust_client.post = self._mock_oauth_response
        self.auth._rust_http_client = self._rust_client

    async def _mock_oauth_response(
        self,
        url: str,
        response_limit: int,
        headers: Mapping[str, str],
        request_body: str,
    ) -> bytes:
        # get the token from the request body which is form encoded
        parsed_body = urllib.parse.parse_qs(request_body)
        token = parsed_body.get("token", [""])[0]

        if not token.startswith("mock_token_"):
            return bytes(json.dumps({"active": False}).encode("utf-8"))
        token = token.replace("mock_token_", "")

        username, device_id = token.split("_", 1)
        user_id = UserID(username, self.hs.hostname)
        store = self.hs.get_datastores().main

        # Check th user exists in the store
        user_info = await store.get_user_by_id(user_id=user_id.to_string())
        if user_info is None:
            return bytes(json.dumps({"active": False}).encode("utf-8"))

        # Check the device exists in the store
        device = await store.get_device(
            user_id=user_id.to_string(), device_id=device_id
        )
        if device is None:
            return bytes(json.dumps({"active": False}).encode("utf-8"))

        return bytes(
            json.dumps(
                {
                    "active": True,
                    "scope": "urn:matrix:client:device:"
                    + device_id
                    + " urn:matrix:client:api:*",
                    "username": username,
                }
            ).encode("utf-8")
        )

    def register_oauth_user(self, username: str, device_id: str) -> str:
        # Provision the user and the device
        store = self.hs.get_datastores().main
        user_id = UserID(username, self.hs.hostname)

        self.get_success(store.register_user(user_id=user_id.to_string()))
        self.get_success(
            store.store_device(
                user_id=user_id.to_string(),
                device_id=device_id,
                initial_device_display_name=None,
            )
        )
        # Generate an access token for the device
        return "mock_token_" + username + "_" + device_id

    def test_disabled(self) -> None:
        channel = self.make_request("POST", msc4108_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)

    @override_config(
        {
            "experimental_features": {
                "msc4108v2025_mode": "off",
            },
        }
    )
    def test_off(self) -> None:
        channel = self.make_request("POST", msc4108_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "public",
            },
        }
    )
    def test_rendezvous_public(self) -> None:
        """
        Test the MSC4108 rendezvous endpoint, including:
            - Creating a session
            - Getting the data back
            - Updating the data
            - Deleting the data
            - Sequence token handling
        """
        # We can post arbitrary data to the endpoint
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]
        expires_ts = channel.json_body["expires_ts"]
        self.assertGreater(expires_ts, self.hs.get_clock().time_msec())

        session_endpoint = msc4108_endpoint + f"/{rendezvous_id}"

        # We can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")
        self.assertEqual(channel.json_body["sequence_token"], sequence_token)
        self.assertEqual(channel.json_body["expires_ts"], expires_ts)

        # We can update the data
        channel = self.make_request(
            "PUT",
            session_endpoint,
            {"sequence_token": sequence_token, "data": "foo=baz"},
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        old_sequence_token = sequence_token
        new_sequence_token = channel.json_body["sequence_token"]

        # If we try to update it again with the old etag, it should fail
        channel = self.make_request(
            "PUT",
            session_endpoint,
            {"sequence_token": old_sequence_token, "data": "bar=baz"},
            access_token=None,
        )

        self.assertEqual(channel.code, 409)
        self.assertEqual(
            channel.json_body["errcode"], "IO_ELEMENT_MSC4108_CONCURRENT_WRITE"
        )

        # We should get the updated data
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=baz")
        self.assertEqual(channel.json_body["sequence_token"], new_sequence_token)
        self.assertEqual(channel.json_body["expires_ts"], expires_ts)

        # We can delete the data
        channel = self.make_request(
            "DELETE",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)

        # If we try to get the data again, it should fail
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "authenticated",
            },
        }
    )
    def test_rendezvous_requires_authentication(self) -> None:
        """
        Test the MSC4108 rendezvous endpoint when configured with the mode authenticated, including:
            - Creating a session
            - Getting the data back
            - Updating the data
            - Deleting the data
            - Sequence token handling
        """
        self.setup_mock_oauth()
        alice_token = self.register_oauth_user("alice", "device1")

        # This should fail without authentication:
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 401)

        # This should work as we are now authenticated
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "foo=bar"},
            access_token=alice_token,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]
        expires_ts = channel.json_body["expires_ts"]
        self.assertGreater(expires_ts, self.hs.get_clock().time_msec())

        session_endpoint = msc4108_endpoint + f"/{rendezvous_id}"

        # We can get the data back without authentication
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")
        self.assertEqual(channel.json_body["sequence_token"], sequence_token)
        self.assertEqual(channel.json_body["expires_ts"], expires_ts)

        # We can update the data without authentication
        channel = self.make_request(
            "PUT",
            session_endpoint,
            {"sequence_token": sequence_token, "data": "foo=baz"},
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        new_sequence_token = channel.json_body["sequence_token"]

        # We should get the updated data without authentication
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=baz")
        self.assertEqual(channel.json_body["sequence_token"], new_sequence_token)
        self.assertEqual(channel.json_body["expires_ts"], expires_ts)

        # We can delete the data without authentication
        channel = self.make_request(
            "DELETE",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)

        # If we try to get the data again, it should fail
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "public",
            },
        }
    )
    def test_expiration(self) -> None:
        """
        Test that entries are evicted after a TTL.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        session_endpoint = msc4108_endpoint + "/" + channel.json_body["id"]

        # Sanity check that we can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")

        # Advance the clock, TTL of entries is 2 minutes
        self.reactor.advance(120)

        # Get the data back, it should be gone
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 404)

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "public",
            },
        }
    )
    def test_capacity(self) -> None:
        """
        Test that a capacity limit is enforced on the rendezvous sessions, as old
        entries are evicted at an interval when the limit is reached.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        session_endpoint = msc4108_endpoint + "/" + channel.json_body["id"]

        # Sanity check that we can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")

        # We advance the clock to make sure that this entry is the "lowest" in the session list
        self.reactor.advance(1)

        # Start a lot of new sessions
        for _ in range(100):
            channel = self.make_request(
                "POST",
                msc4108_endpoint,
                {"data": "foo=bar"},
                access_token=None,
            )
            self.assertEqual(channel.code, 200)

        # Get the data back, it should still be there, as the eviction hasn't run yet
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)

        # Advance the clock, as it will trigger the eviction
        self.reactor.advance(59)

        # Get the data back, it should be gone
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 404)

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "public",
            },
        }
    )
    def test_hard_capacity(self) -> None:
        """
        Test that a hard capacity limit is enforced on the rendezvous sessions, as old
        entries are evicted immediately when the limit is reached.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        session_endpoint = msc4108_endpoint + "/" + channel.json_body["id"]
        # We advance the clock to make sure that this entry is the "lowest" in the session list
        self.reactor.advance(1)

        # Sanity check that we can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")

        # Start a lot of new sessions
        for _ in range(200):
            channel = self.make_request(
                "POST",
                msc4108_endpoint,
                {"data": "foo=bar"},
                access_token=None,
            )
            self.assertEqual(channel.code, 200)

        # Get the data back, it should already be gone as we hit the hard limit
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 404)

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "public",
            },
        }
    )
    def test_data_type(self) -> None:
        """
        Test that the data field is restricted to string.
        """
        invalid_datas: list[Any] = [123214, ["asd"], {"asd": "asdsad"}, None]

        # We cannot post invalid non-string data field values to the endpoint
        for invalid_data in invalid_datas:
            channel = self.make_request(
                "POST",
                msc4108_endpoint,
                {"data": invalid_data},
                access_token=None,
            )
            self.assertEqual(channel.code, 400)
            self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

        # Make a valid request
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "test"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]

        session_endpoint = msc4108_endpoint + f"/{rendezvous_id}"

        # We can't update the data with invalid data
        for invalid_data in invalid_datas:
            channel = self.make_request(
                "PUT",
                session_endpoint,
                {"sequence_token": sequence_token, "data": invalid_data},
                access_token=None,
            )
            self.assertEqual(channel.code, 400)
            self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "matrix_authentication_service": {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
            "experimental_features": {
                "msc4108v2025_mode": "public",
            },
        }
    )
    def test_max_length(self) -> None:
        """
        Test that the data max length is restricted.
        """
        too_long_data = "a" * 5000  # MSC4108 specifies 4KB max length

        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": too_long_data},
            access_token=None,
        )
        self.assertEqual(channel.code, 413)
        self.assertEqual(channel.json_body["errcode"], "M_TOO_LARGE")

        # Make a valid request
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            {"data": "test"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]

        session_endpoint = msc4108_endpoint + f"/{rendezvous_id}"

        # We can't update the data with invalid data
        channel = self.make_request(
            "PUT",
            session_endpoint,
            {"sequence_token": sequence_token, "data": too_long_data},
            access_token=None,
        )
        self.assertEqual(channel.code, 413)
        self.assertEqual(channel.json_body["errcode"], "M_TOO_LARGE")
