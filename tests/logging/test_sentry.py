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
#

import hashlib
from typing import TYPE_CHECKING

import unpaddedbase64

from synapse.logging.context import ContextRequest, LoggingContext
from synapse.logging.sentry import before_send
from synapse.metrics.background_process_metrics import BackgroundProcessLoggingContext

from tests.unittest import TestCase

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

SERVER_NAME = "test_server"


def make_context_request(**kwargs: object) -> ContextRequest:
    """Build a `ContextRequest` that looks like one from a real request."""
    defaults: dict = {
        "request_id": "GET-42",
        "ip_address": "1.2.3.4",
        "site_tag": "8008",
        "requester": "@alice:example.com",
        "authenticated_entity": "@alice:example.com",
        "method": "GET",
        "url": "/_matrix/client/v3/rooms/!room:example.com/messages?from=s1&limit=10",
        "protocol": "HTTP/1.1",
        "user_agent": "Element/1.0",
        "servlet_name": "RoomMessageListRestServlet",
    }
    defaults.update(kwargs)
    return ContextRequest(**defaults)


def pseudonym(identifier: str) -> str:
    """The pseudonymous id `before_send` is expected to derive from `identifier`."""
    digest = hashlib.sha256(identifier.encode("utf-8")).digest()
    return unpaddedbase64.encode_base64(digest, urlsafe=True)[:16]


class BeforeSendTestCase(TestCase):
    def test_request_attached(self) -> None:
        """Request details from the logcontext are mapped onto the event."""
        request = make_context_request()
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(name="GET-42", server_name=SERVER_NAME, request=request):
            result = before_send(event, hint)

        assert result is not None

        self.assertEqual(result["transaction"], "RoomMessageListRestServlet")
        self.assertEqual(
            result["tags"],
            {
                "servlet": "RoomMessageListRestServlet",
                "site_tag": "8008",
                "method": "GET",
            },
        )
        self.assertEqual(
            result["contexts"]["synapse_request"],
            {
                "request_id": "GET-42",
                "servlet": "RoomMessageListRestServlet",
                "protocol": "HTTP/1.1",
            },
        )
        self.assertEqual(
            result["request"],
            {
                "method": "GET",
                # The query string is dropped.
                "url": "/_matrix/client/v3/rooms/!room:example.com/messages",
                "headers": {"User-Agent": "Element/1.0"},
            },
        )

    def test_user_is_pseudonymous(self) -> None:
        """The Matrix ID is replaced by a stable hash of itself."""
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(
            name="GET-42", server_name=SERVER_NAME, request=make_context_request()
        ):
            result = before_send(event, hint)

        assert result is not None
        self.assertEqual(result["user"], {"id": pseudonym("@alice:example.com")})

    def test_puppeted_user_is_the_pseudonymous_user(self) -> None:
        """When an admin puppets a user, the event names the puppeted user."""
        request = make_context_request(
            requester="@bob:example.com",
            authenticated_entity="@admin:example.com",
        )
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(name="PUT-1", server_name=SERVER_NAME, request=request):
            result = before_send(event, hint)

        assert result is not None
        self.assertEqual(result["user"], {"id": pseudonym("@bob:example.com")})
        self.assertNotIn("@admin:example.com", repr(result))

    def test_federation_requester_is_the_origin_server(self) -> None:
        """A requester that isn't a Matrix ID is a server name, kept as context."""
        request = make_context_request(
            requester="remote.example.com",
            authenticated_entity="remote.example.com",
        )
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(name="PUT-1", server_name=SERVER_NAME, request=request):
            result = before_send(event, hint)

        assert result is not None
        self.assertNotIn("user", result)
        self.assertEqual(
            result["contexts"]["synapse_request"]["requester"], "remote.example.com"
        )

    def test_unauthenticated_request_has_no_user(self) -> None:
        """A request that never authenticated identifies no user at all."""
        request = make_context_request(requester=None, authenticated_entity=None)
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(name="GET-1", server_name=SERVER_NAME, request=request):
            result = before_send(event, hint)

        assert result is not None
        self.assertNotIn("user", result)

    def test_ip_address_is_not_sent(self) -> None:
        """The client's IP address is nowhere on the event."""
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(
            name="GET-42", server_name=SERVER_NAME, request=make_context_request()
        ):
            result = before_send(event, hint)

        assert result is not None
        self.assertNotIn("1.2.3.4", repr(result))

    def test_logcontext_extra_is_stripped(self) -> None:
        """The attributes `LoggingContextFilter` puts on records don't reach Sentry."""
        event: "Event" = {
            "extra": {
                "request": "GET-42",
                "server_name": SERVER_NAME,
                "ip_address": "1.2.3.4",
                "site_tag": "8008",
                "requester": "@alice:example.com",
                "authenticated_entity": "@alice:example.com",
                "method": "GET",
                "url": "/_matrix/client/v3/sync",
                "protocol": "HTTP/1.1",
                "user_agent": "Element/1.0",
                "sys.argv": ["synapse"],
                "asctime": "2026-01-01 00:00:00",
                "something_useful": 1,
            }
        }
        hint: "Hint" = {}

        with LoggingContext(
            name="GET-42", server_name=SERVER_NAME, request=make_context_request()
        ):
            result = before_send(event, hint)

        assert result is not None
        self.assertEqual(result["extra"], {"something_useful": 1})

    def test_background_process(self) -> None:
        """Background processes are tagged with their description, not their id."""
        event: "Event" = {}
        hint: "Hint" = {}

        with BackgroundProcessLoggingContext(
            name="update_user_directory", server_name=SERVER_NAME, instance_id=1
        ):
            result = before_send(event, hint)

        assert result is not None
        self.assertEqual(
            result["tags"], {"background_process": "update_user_directory"}
        )
        self.assertNotIn("request", result)

    def test_sentinel_context(self) -> None:
        """Events captured outside any logcontext are passed through untouched."""
        event: "Event" = {"extra": {"asctime": "now"}}
        hint: "Hint" = {}

        result = before_send(event, hint)

        assert result is not None
        self.assertEqual(result, {"extra": {}})
