#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
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

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.auth.mas import MasDelegatedAuth
from synapse.rest import admin
from synapse.rest.client import login, rendezvous
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import checked_cast, override_config

rz_endpoint = "/_matrix/client/unstable/io.element.msc4388/rendezvous"


def send_endpoint(rendezvous_id: str, txn_id: str) -> str:
    """The endpoint for sending a payload, which includes a transaction ID."""
    return f"{rz_endpoint}/{rendezvous_id}/{txn_id}"


class RendezvousDisabledTestCase(unittest.HomeserverTestCase):
    """
    Test that the experimental MSC4388 rendezvous endpoint is disabled by
    default.
    """

    servlets = [rendezvous.register_servlets]

    def test_disabled(self) -> None:
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)
        channel = self.make_request("POST", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)


class RendezvousServletTestCase(unittest.HomeserverTestCase):
    """
    Test the experimental MSC4388 rendezvous endpoint.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        rendezvous.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()

        # By default, tests run with MAS and MSC4388 in open mode. Tests can
        # override individual experimental features, as `override_config`
        # replaces `experimental_features` wholesale, so the defaults are
        # merged in here.
        config.setdefault("disable_registration", True)
        config.setdefault(
            "matrix_authentication_service",
            {
                "enabled": True,
                "secret": "secret_value",
                "endpoint": "https://issuer",
            },
        )
        config["experimental_features"] = {
            "msc4388_mode": "open",
            # Effectively disable ratelimiting, as some tests make a large
            # number of requests, unless the test configures it.
            "msc4388_rc_create": {"per_second": 10000, "burst_count": 10000},
            "msc4388_rc_requests": {"per_second": 10000, "burst_count": 10000},
            **(config.get("experimental_features") or {}),
        }

        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()
        return self.hs

    def setup_mock_oauth(self) -> None:
        """
        This isn't a very elegant way to mock the OAuth API, but it works for our purposes.
        """

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

    @override_config(
        {
            "experimental_features": {
                "msc4388_mode": "off",
            },
        }
    )
    def test_off(self) -> None:
        # Discovery endpoint should return 404
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)
        # Create session should also fail
        channel = self.make_request("POST", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)

    def test_rendezvous_public_with_access_token(self) -> None:
        """
        Test that when authentication is not required, a valid access token is
        accepted but an invalid one is rejected rather than ignored.
        """
        self.setup_mock_oauth()
        alice_token = self.register_oauth_user("alice", "device1")

        # Discovery and creating a session should work with a valid token
        channel = self.make_request("GET", rz_endpoint, {}, access_token=alice_token)
        self.assertEqual(channel.code, 200)
        self.assertTrue(channel.json_body.get("create_available"))

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=alice_token,
        )
        self.assertEqual(channel.code, 200)

        # Discovery and creating a session should fail with an invalid token,
        # rather than the request being treated as unauthenticated
        channel = self.make_request(
            "GET", rz_endpoint, {}, access_token="invalid_token"
        )
        self.assertEqual(channel.code, 401)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token="invalid_token",
        )
        self.assertEqual(channel.code, 401)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")

    def test_rendezvous_public(self) -> None:
        """
        Test the MSC4388 rendezvous endpoint, including:
            - Creating a session
            - Getting the data back
            - Updating the data
            - Deleting the data
            - Sequence token handling
        """
        # Discovery should return 200
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 200)
        self.assertTrue(channel.json_body.get("create_available"))

        # We can post arbitrary data to the endpoint
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]
        self.assertGreater(channel.json_body["expires_in_ms"], 0)

        session_endpoint = rz_endpoint + f"/{rendezvous_id}"

        # We can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")
        self.assertEqual(channel.json_body["sequence_token"], sequence_token)
        self.assertGreater(channel.json_body["expires_in_ms"], 0)

        # We can update the data
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": sequence_token, "data": "foo=baz"},
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        old_sequence_token = sequence_token
        new_sequence_token = channel.json_body["sequence_token"]
        self.assertNotEqual(new_sequence_token, old_sequence_token)

        # If we try to update it again with the old sequence token, it should fail
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn2"),
            {"sequence_token": old_sequence_token, "data": "bar=baz"},
            access_token=None,
        )

        self.assertEqual(channel.code, 409)
        self.assertEqual(
            channel.json_body["errcode"], "IO_ELEMENT_MSC4388_CONCURRENT_WRITE"
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
        self.assertGreater(channel.json_body["expires_in_ms"], 0)

        # We can delete the data
        channel = self.make_request(
            "DELETE",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {})

        # If we try to get the data again, it should fail
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_rendezvous_put_is_idempotent(self) -> None:
        """
        A PUT which reuses a transaction ID the server has already seen for the
        session is answered with the response recorded for the original
        request, without the payload being advanced. This lets clients safely
        retry a PUT whose response was lost.
        """
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        initial_sequence_token = channel.json_body["sequence_token"]
        session_endpoint = rz_endpoint + f"/{rendezvous_id}"

        # Perform an update.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": initial_sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        updated_sequence_token = channel.json_body["sequence_token"]

        # Replaying the same PUT with the same transaction ID should succeed
        # and return the same sequence token, without advancing the payload.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": initial_sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["sequence_token"], updated_sequence_token)

        # A transaction ID identifies a single send attempt rather than a
        # payload, so reusing it with different data replays the recorded
        # response and the new payload is not considered.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": updated_sequence_token, "data": "something=else"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["sequence_token"], updated_sequence_token)

        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=baz")
        self.assertEqual(channel.json_body["sequence_token"], updated_sequence_token)

        # A new transaction ID with a stale sequence token is rejected as a
        # concurrent write...
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn2"),
            {"sequence_token": initial_sequence_token, "data": "something=else"},
            access_token=None,
        )
        self.assertEqual(channel.code, 409)
        self.assertEqual(
            channel.json_body["errcode"], "IO_ELEMENT_MSC4388_CONCURRENT_WRITE"
        )

        # ...and retrying it gets the same error.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn2"),
            {"sequence_token": initial_sequence_token, "data": "something=else"},
            access_token=None,
        )
        self.assertEqual(channel.code, 409)
        self.assertEqual(
            channel.json_body["errcode"], "IO_ELEMENT_MSC4388_CONCURRENT_WRITE"
        )

        # The stored data should be unchanged.
        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=baz")
        self.assertEqual(channel.json_body["sequence_token"], updated_sequence_token)

        # A fresh transaction ID with the current sequence token is accepted.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn3"),
            {"sequence_token": updated_sequence_token, "data": "something=else"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertNotEqual(channel.json_body["sequence_token"], updated_sequence_token)

    def test_rendezvous_put_rejected_writes_are_not_recorded(self) -> None:
        """
        Only successful PUTs are recorded against their transaction ID, so a
        large number of rejected writes cannot push the record of a successful
        one out of the bounded per-session history and turn a legitimate retry
        into a spurious concurrent write error.
        """
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        initial_sequence_token = channel.json_body["sequence_token"]

        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": initial_sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        updated_sequence_token = channel.json_body["sequence_token"]

        # More rejected writes than the server remembers transactions for.
        for i in range(150):
            channel = self.make_request(
                "PUT",
                send_endpoint(rendezvous_id, f"rejected{i}"),
                {"sequence_token": initial_sequence_token, "data": "something=else"},
                access_token=None,
            )
            self.assertEqual(channel.code, 409)

        # The original PUT can still be retried idempotently.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": initial_sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["sequence_token"], updated_sequence_token)

    def test_rendezvous_put_transaction_id(self) -> None:
        """
        The transaction ID must be supplied in the path and comply with the
        opaque identifier grammar.
        """
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]

        # A PUT without a transaction ID is not a supported request.
        channel = self.make_request(
            "PUT",
            rz_endpoint + f"/{rendezvous_id}",
            {"sequence_token": sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 405)
        self.assertEqual(channel.json_body["errcode"], "M_UNRECOGNIZED")

        # Transaction IDs which don't comply with the opaque identifier grammar
        # are rejected.
        for invalid_txn_id in ("txn!1", "txn=1", "a" * 256, "txn%201"):
            channel = self.make_request(
                "PUT",
                send_endpoint(rendezvous_id, invalid_txn_id),
                {"sequence_token": sequence_token, "data": "foo=baz"},
                access_token=None,
            )
            self.assertEqual(channel.code, 400, invalid_txn_id)
            self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

        # The payload should not have been updated by any of the above.
        channel = self.make_request(
            "GET",
            rz_endpoint + f"/{rendezvous_id}",
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")
        self.assertEqual(channel.json_body["sequence_token"], sequence_token)

        # Any transaction ID which does comply with the grammar is accepted,
        # including the maximum length and the full set of allowed characters.
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "Txn-1._~" + "a" * 247),
            {"sequence_token": sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)

    @override_config({"experimental_features": {"msc4388_mode": "authenticated"}})
    def test_rendezvous_requires_authentication(self) -> None:
        """
        Test the MSC4388 rendezvous endpoint when configured with the mode
        authenticated: discovery and creating a session require authentication,
        but using the session does not.

        The rest of the session lifecycle is covered by test_rendezvous_public.
        """
        self.setup_mock_oauth()
        alice_token = self.register_oauth_user("alice", "device1")

        # Discovery should say that we can't create a session without
        # authentication, rather than returning an error
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 200)
        self.assertIs(channel.json_body.get("create_available"), False)

        # Creating a session should be forbidden without authentication
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 403)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")

        # Discovery and creating a session should fail with an invalid token,
        # so that the client can tell that it needs to refresh its token
        channel = self.make_request(
            "GET", rz_endpoint, {}, access_token="invalid_token"
        )
        self.assertEqual(channel.code, 401)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token="invalid_token",
        )
        self.assertEqual(channel.code, 401)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")

        # Discovery should succeed with authentication
        channel = self.make_request("GET", rz_endpoint, {}, access_token=alice_token)
        self.assertEqual(channel.code, 200)
        self.assertTrue(channel.json_body.get("create_available"))

        # This should work as we are now authenticated
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=alice_token,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]

        # The other device can use the session without authentication
        channel = self.make_request(
            "GET",
            rz_endpoint + f"/{rendezvous_id}",
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")
        self.assertEqual(channel.json_body["sequence_token"], sequence_token)

    def test_expiration(self) -> None:
        """
        Test that entries are evicted after a TTL.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        session_endpoint = rz_endpoint + "/" + channel.json_body["id"]

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

    @override_config({"experimental_features": {"msc4388_session_ttl": "5m"}})
    def test_configured_ttl(self) -> None:
        """
        Test that sessions expire after the configured TTL.
        """
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["expires_in_ms"], 5 * 60 * 1000)
        session_endpoint = rz_endpoint + "/" + channel.json_body["id"]

        # The session is still there after the default TTL of 2 minutes...
        self.reactor.advance(120)
        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 200)
        # (allowing for the time taken by the requests)
        self.assertLessEqual(channel.json_body["expires_in_ms"], 3 * 60 * 1000)
        self.assertGreater(channel.json_body["expires_in_ms"], 3 * 60 * 1000 - 1000)

        # ...but is gone after the configured TTL of 5 minutes
        self.reactor.advance(180)
        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 404)

    def test_delete_expired(self) -> None:
        """
        Test that deleting a session which has expired, but has not yet been
        evicted, fails as if the session did not exist.
        """
        # Offset the session from the eviction interval (60 seconds), so that
        # it expires between two eviction runs
        self.reactor.advance(30)

        # Start a new session
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        session_endpoint = rz_endpoint + "/" + channel.json_body["id"]

        # Advance the clock to just before the TTL of entries (2 minutes). Any
        # overdue eviction run happens at the end of an advance, so it does so
        # while the session is still live.
        self.reactor.advance(119)

        # Then advance the clock to the TTL, which is before the next eviction
        # run
        self.reactor.advance(1)

        # Deleting the session should fail as it has expired
        channel = self.make_request(
            "DELETE",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 404)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    @override_config({"experimental_features": {"msc4388_max_sessions": 10}})
    def test_capacity(self) -> None:
        """
        Test that new sessions are refused while at capacity, rather than live
        sessions being evicted, and that capacity is freed up as sessions expire.
        """
        # Offset the sessions from the eviction interval (60 seconds), so that
        # they expire between two eviction runs
        self.reactor.advance(30)

        # Start a new session
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        session_endpoint = rz_endpoint + "/" + channel.json_body["id"]

        # Fill up the remaining capacity
        for _ in range(9):
            channel = self.make_request(
                "POST",
                rz_endpoint,
                {"data": "foo=bar"},
                access_token=None,
            )
            self.assertEqual(channel.code, 200)

        # Further sessions are refused
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 429)
        self.assertEqual(channel.json_body["errcode"], "M_LIMIT_EXCEEDED")

        # The first session is still there, even after the eviction has run
        self.reactor.advance(60)
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=bar")

        # Deleting a session frees up capacity for a new one
        channel = self.make_request(
            "DELETE",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)

        # Once the original sessions have expired, there is capacity for new
        # ones even though the eviction has not run since. The session created
        # after the delete is still live, so only 9 fit.
        self.reactor.advance(61)
        for _ in range(9):
            channel = self.make_request(
                "POST",
                rz_endpoint,
                {"data": "foo=bar"},
                access_token=None,
            )
            self.assertEqual(channel.code, 200)

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 429)

    def test_data_type(self) -> None:
        """
        Test that the data field is restricted to string.
        """
        invalid_datas: list[Any] = [123214, ["asd"], {"asd": "asdsad"}, None]

        # We cannot post invalid non-string data field values to the endpoint
        for invalid_data in invalid_datas:
            channel = self.make_request(
                "POST",
                rz_endpoint,
                {"data": invalid_data},
                access_token=None,
            )
            self.assertEqual(channel.code, 400)
            self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

        # Make a valid request
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "test"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]

        # We can't update the data with invalid data
        for i, invalid_data in enumerate(invalid_datas):
            channel = self.make_request(
                "PUT",
                send_endpoint(rendezvous_id, f"txn{i}"),
                {"sequence_token": sequence_token, "data": invalid_data},
                access_token=None,
            )
            self.assertEqual(channel.code, 400)
            self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

    def test_max_length(self) -> None:
        """
        Test that the data max length is restricted.
        """
        too_long_data = "a" * 5000  # MSC4388 specifies 4KB max length

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": too_long_data},
            access_token=None,
        )
        self.assertEqual(channel.code, 413)
        self.assertEqual(channel.json_body["errcode"], "M_TOO_LARGE")

        # Make a valid request
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "test"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]

        # We can't update the data with invalid data
        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": sequence_token, "data": too_long_data},
            access_token=None,
        )
        self.assertEqual(channel.code, 413)
        self.assertEqual(channel.json_body["errcode"], "M_TOO_LARGE")

    @parameterized.expand(
        [
            ("Sec-Fetch-Dest", "document"),
            ("Sec-Fetch-Dest", "image"),
            ("Sec-Fetch-Dest", "iframe"),
            ("Sec-Fetch-Dest", "embed"),
            ("Sec-Fetch-Dest", "video"),
            ("Sec-Fetch-Mode", "navigate"),
            ("Sec-Fetch-User", "?1"),
            ("Sec-Fetch-Site", "none"),
        ]
    )
    def test_rendezvous_rejects_unsafe_get_requests(
        self, header_name: str, header_value: str
    ) -> None:
        """
        Tests that GET requests have the appropriate Sec-Fetch-* controls applied as per the MSC.
        The mode is set to `public` but this doesn't actually matter.
        """
        # We can post arbitrary data to the endpoint
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]

        session_endpoint = rz_endpoint + f"/{rendezvous_id}"

        # We can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)

        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
            custom_headers=[(header_name, header_value)],
        )
        self.assertEqual(channel.code, 403)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")

    def test_rendezvous_allows_from_browser_fetch(self) -> None:
        """
        We check that the GET policy does allow for an expected browser fetch
        The mode is set to `public` but this doesn't actually matter.
        """
        # We can post arbitrary data to the endpoint
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]

        session_endpoint = rz_endpoint + f"/{rendezvous_id}"

        # We can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)

        # Test for a typical browser fetch from a client hosted on a different origin
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
            custom_headers=[
                ("Sec-Fetch-Dest", "empty"),
                ("Sec-Fetch-Mode", "cors"),
                ("Sec-Fetch-Site", "cross-site"),
            ],
        )

        self.assertEqual(channel.code, 200)

    @override_config(
        {
            "experimental_features": {
                "msc4388_rc_create": {"per_second": 0.1, "burst_count": 2},
            },
        }
    )
    def test_create_ratelimited(self) -> None:
        """
        Test that creating a session is ratelimited, separately from other
        requests.
        """
        for _ in range(2):
            channel = self.make_request(
                "POST",
                rz_endpoint,
                {"data": "foo=bar"},
                access_token=None,
            )
            self.assertEqual(channel.code, 200)

        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 429)
        self.assertEqual(channel.json_body["errcode"], "M_LIMIT_EXCEEDED")

        # Other requests are not affected
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 200)

        # Once enough time has passed, a session can be created again
        self.reactor.advance(10)
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)

    @override_config(
        {
            "experimental_features": {
                "msc4388_rc_requests": {"per_second": 0.1, "burst_count": 4},
            },
        }
    )
    def test_requests_ratelimited(self) -> None:
        """
        Test that discovery, and receiving, sending and cancelling on a session
        share a ratelimit.
        """
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        rendezvous_id = channel.json_body["id"]
        sequence_token = channel.json_body["sequence_token"]
        session_endpoint = rz_endpoint + f"/{rendezvous_id}"

        # Use up the burst with one request to each endpoint
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 200)

        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 200)

        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn1"),
            {"sequence_token": sequence_token, "data": "foo=baz"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        sequence_token = channel.json_body["sequence_token"]

        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 200)

        # Now every one of them is ratelimited
        channel = self.make_request("GET", rz_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 429)
        self.assertEqual(channel.json_body["errcode"], "M_LIMIT_EXCEEDED")

        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 429)
        self.assertEqual(channel.json_body["errcode"], "M_LIMIT_EXCEEDED")

        channel = self.make_request(
            "PUT",
            send_endpoint(rendezvous_id, "txn2"),
            {"sequence_token": sequence_token, "data": "foo=qux"},
            access_token=None,
        )
        self.assertEqual(channel.code, 429)
        self.assertEqual(channel.json_body["errcode"], "M_LIMIT_EXCEEDED")

        channel = self.make_request("DELETE", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 429)
        self.assertEqual(channel.json_body["errcode"], "M_LIMIT_EXCEEDED")

        # Creating a session is not affected
        channel = self.make_request(
            "POST",
            rz_endpoint,
            {"data": "foo=bar"},
            access_token=None,
        )
        self.assertEqual(channel.code, 200)

        # Once enough time has passed, requests are accepted again, and the
        # ratelimited PUT did not change the payload
        self.reactor.advance(10)
        channel = self.make_request("GET", session_endpoint, access_token=None)
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body["data"], "foo=baz")
        self.assertEqual(channel.json_body["sequence_token"], sequence_token)
