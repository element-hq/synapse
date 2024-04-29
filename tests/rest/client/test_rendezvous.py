#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023-2024 New Vector, Ltd
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

from typing import Dict
from urllib.parse import urlparse

from twisted.test.proto_helpers import MemoryReactor
from twisted.web.resource import Resource

from synapse.rest.client import rendezvous
from synapse.rest.synapse.client.rendezvous import MSC4108RendezvousSessionResource
from synapse.server import HomeServer
from synapse.util import Clock

from tests import unittest
from tests.unittest import override_config
from tests.utils import HAS_AUTHLIB

msc3886_endpoint = "/_matrix/client/unstable/org.matrix.msc3886/rendezvous"
msc4108_endpoint = "/_matrix/client/unstable/org.matrix.msc4108/rendezvous"


class RendezvousServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        rendezvous.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()
        return self.hs

    def create_resource_dict(self) -> Dict[str, Resource]:
        return {
            **super().create_resource_dict(),
            "/_synapse/client/rendezvous": MSC4108RendezvousSessionResource(self.hs),
        }

    def test_disabled(self) -> None:
        channel = self.make_request("POST", msc3886_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)
        channel = self.make_request("POST", msc4108_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 404)

    @override_config({"experimental_features": {"msc3886_endpoint": "/asd"}})
    def test_msc3886_redirect(self) -> None:
        channel = self.make_request("POST", msc3886_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 307)
        self.assertEqual(channel.headers.getRawHeaders("Location"), ["/asd"])

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "experimental_features": {
                "msc4108_delegation_endpoint": "https://asd",
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "client_id": "client_id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "client_secret",
                    "admin_token": "admin_token_value",
                },
            },
        }
    )
    def test_msc4108_delegation(self) -> None:
        channel = self.make_request("POST", msc4108_endpoint, {}, access_token=None)
        self.assertEqual(channel.code, 307)
        self.assertEqual(channel.headers.getRawHeaders("Location"), ["https://asd"])

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "experimental_features": {
                "msc4108_enabled": True,
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "client_id": "client_id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "client_secret",
                    "admin_token": "admin_token_value",
                },
            },
        }
    )
    def test_msc4108(self) -> None:
        """
        Test the MSC4108 rendezvous endpoint, including:
            - Creating a session
            - Getting the data back
            - Updating the data
            - Deleting the data
            - ETag handling
        """
        # We can post arbitrary data to the endpoint
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            "foo=bar",
            content_type=b"text/plain",
            access_token=None,
        )
        self.assertEqual(channel.code, 201)
        self.assertSubstring("/_synapse/client/rendezvous/", channel.json_body["url"])
        headers = dict(channel.headers.getAllRawHeaders())
        self.assertIn(b"ETag", headers)
        self.assertIn(b"Expires", headers)
        self.assertEqual(headers[b"Content-Type"], [b"application/json"])
        self.assertEqual(headers[b"Access-Control-Allow-Origin"], [b"*"])
        self.assertEqual(headers[b"Access-Control-Expose-Headers"], [b"etag"])
        self.assertEqual(headers[b"Cache-Control"], [b"no-store"])
        self.assertEqual(headers[b"Pragma"], [b"no-cache"])
        self.assertIn("url", channel.json_body)
        self.assertTrue(channel.json_body["url"].startswith("https://"))

        url = urlparse(channel.json_body["url"])
        session_endpoint = url.path
        etag = headers[b"ETag"][0]

        # We can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)
        headers = dict(channel.headers.getAllRawHeaders())
        self.assertEqual(headers[b"ETag"], [etag])
        self.assertIn(b"Expires", headers)
        self.assertEqual(headers[b"Content-Type"], [b"text/plain"])
        self.assertEqual(headers[b"Access-Control-Allow-Origin"], [b"*"])
        self.assertEqual(headers[b"Access-Control-Expose-Headers"], [b"etag"])
        self.assertEqual(headers[b"Cache-Control"], [b"no-store"])
        self.assertEqual(headers[b"Pragma"], [b"no-cache"])
        self.assertEqual(channel.text_body, "foo=bar")

        # We can make sure the data hasn't changed
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
            custom_headers=[("If-None-Match", etag)],
        )

        self.assertEqual(channel.code, 304)

        # We can update the data
        channel = self.make_request(
            "PUT",
            session_endpoint,
            "foo=baz",
            content_type=b"text/plain",
            access_token=None,
            custom_headers=[("If-Match", etag)],
        )

        self.assertEqual(channel.code, 202)
        headers = dict(channel.headers.getAllRawHeaders())
        old_etag = etag
        new_etag = headers[b"ETag"][0]

        # If we try to update it again with the old etag, it should fail
        channel = self.make_request(
            "PUT",
            session_endpoint,
            "bar=baz",
            content_type=b"text/plain",
            access_token=None,
            custom_headers=[("If-Match", old_etag)],
        )

        self.assertEqual(channel.code, 412)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN")
        self.assertEqual(
            channel.json_body["org.matrix.msc4108.errcode"], "M_CONCURRENT_WRITE"
        )

        # If we try to get with the old etag, we should get the updated data
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
            custom_headers=[("If-None-Match", old_etag)],
        )

        self.assertEqual(channel.code, 200)
        headers = dict(channel.headers.getAllRawHeaders())
        self.assertEqual(headers[b"ETag"], [new_etag])
        self.assertEqual(channel.text_body, "foo=baz")

        # We can delete the data
        channel = self.make_request(
            "DELETE",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 204)

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
            "experimental_features": {
                "msc4108_enabled": True,
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "client_id": "client_id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "client_secret",
                    "admin_token": "admin_token_value",
                },
            },
        }
    )
    def test_msc4108_expiration(self) -> None:
        """
        Test that entries are evicted after a TTL.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            "foo=bar",
            content_type=b"text/plain",
            access_token=None,
        )
        self.assertEqual(channel.code, 201)
        session_endpoint = urlparse(channel.json_body["url"]).path

        # Sanity check that we can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.text_body, "foo=bar")

        # Advance the clock, TTL of entries is 1 minute
        self.reactor.advance(60)

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
            "experimental_features": {
                "msc4108_enabled": True,
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "client_id": "client_id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "client_secret",
                    "admin_token": "admin_token_value",
                },
            },
        }
    )
    def test_msc4108_capacity(self) -> None:
        """
        Test that a capacity limit is enforced on the rendezvous sessions, as old
        entries are evicted at an interval when the limit is reached.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            "foo=bar",
            content_type=b"text/plain",
            access_token=None,
        )
        self.assertEqual(channel.code, 201)
        session_endpoint = urlparse(channel.json_body["url"]).path

        # Sanity check that we can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.text_body, "foo=bar")

        # Start a lot of new sessions
        for _ in range(100):
            channel = self.make_request(
                "POST",
                msc4108_endpoint,
                "foo=bar",
                content_type=b"text/plain",
                access_token=None,
            )
            self.assertEqual(channel.code, 201)

        # Get the data back, it should still be there, as the eviction hasn't run yet
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

        self.assertEqual(channel.code, 200)

        # Advance the clock, as it will trigger the eviction
        self.reactor.advance(1)

        # Get the data back, it should be gone
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )

    @unittest.skip_unless(HAS_AUTHLIB, "requires authlib")
    @override_config(
        {
            "disable_registration": True,
            "experimental_features": {
                "msc4108_enabled": True,
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "client_id": "client_id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "client_secret",
                    "admin_token": "admin_token_value",
                },
            },
        }
    )
    def test_msc4108_hard_capacity(self) -> None:
        """
        Test that a hard capacity limit is enforced on the rendezvous sessions, as old
        entries are evicted immediately when the limit is reached.
        """
        # Start a new session
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            "foo=bar",
            content_type=b"text/plain",
            access_token=None,
        )
        self.assertEqual(channel.code, 201)
        session_endpoint = urlparse(channel.json_body["url"]).path
        # We advance the clock to make sure that this entry is the "lowest" in the session list
        self.reactor.advance(1)

        # Sanity check that we can get the data back
        channel = self.make_request(
            "GET",
            session_endpoint,
            access_token=None,
        )
        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.text_body, "foo=bar")

        # Start a lot of new sessions
        for _ in range(200):
            channel = self.make_request(
                "POST",
                msc4108_endpoint,
                "foo=bar",
                content_type=b"text/plain",
                access_token=None,
            )
            self.assertEqual(channel.code, 201)

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
            "experimental_features": {
                "msc4108_enabled": True,
                "msc3861": {
                    "enabled": True,
                    "issuer": "https://issuer",
                    "client_id": "client_id",
                    "client_auth_method": "client_secret_post",
                    "client_secret": "client_secret",
                    "admin_token": "admin_token_value",
                },
            },
        }
    )
    def test_msc4108_content_type(self) -> None:
        """
        Test that the content-type is restricted to text/plain.
        """
        # We cannot post invalid content-type arbitrary data to the endpoint
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            "foo=bar",
            content_is_form=True,
            access_token=None,
        )
        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

        # Make a valid request
        channel = self.make_request(
            "POST",
            msc4108_endpoint,
            "foo=bar",
            content_type=b"text/plain",
            access_token=None,
        )
        self.assertEqual(channel.code, 201)
        url = urlparse(channel.json_body["url"])
        session_endpoint = url.path
        headers = dict(channel.headers.getAllRawHeaders())
        etag = headers[b"ETag"][0]

        # We can't update the data with invalid content-type
        channel = self.make_request(
            "PUT",
            session_endpoint,
            "foo=baz",
            content_is_form=True,
            access_token=None,
            custom_headers=[("If-Match", etag)],
        )
        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")
