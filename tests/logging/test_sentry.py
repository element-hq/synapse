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
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import sentry_sdk
import unpaddedbase64
from sentry_sdk.envelope import Envelope
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.transport import Transport

from synapse.logging import context as logcontext
from synapse.logging.context import ContextRequest, LoggingContext
from synapse.logging.sentry import before_breadcrumb, before_send, sentry_sdk_options
from synapse.metrics.background_process_metrics import BackgroundProcessLoggingContext

from tests.unittest import TestCase

if TYPE_CHECKING:
    from sentry_sdk.types import Breadcrumb, Event, Hint

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


class FingerprintTestCase(TestCase):
    def _make_record(self) -> logging.LogRecord:
        return logging.LogRecord(
            name="synapse.storage.databases.main.event_federation",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="Unexpectedly found that events don't have chain IDs in room %s: %s",
            args=("!room:example.com", "$event"),
            exc_info=None,
        )

    def test_message_event_grouped_by_template(self) -> None:
        """Message events group on the logger name and the unformatted template."""
        event: "Event" = {}
        hint: "Hint" = {"log_record": self._make_record()}

        result = before_send(event, hint)

        assert result is not None
        self.assertEqual(
            result["fingerprint"],
            [
                "synapse.storage.databases.main.event_federation",
                "Unexpectedly found that events don't have chain IDs in room %s: %s",
            ],
        )

    def test_exception_event_keeps_default_grouping(self) -> None:
        """Exceptions group on their stack trace, which is already specific."""
        event: "Event" = {"exception": {"values": []}}
        hint: "Hint" = {"log_record": self._make_record()}

        result = before_send(event, hint)

        assert result is not None
        self.assertNotIn("fingerprint", result)

    def test_event_without_log_record(self) -> None:
        """Events that didn't come from a log record are left to group themselves."""
        event: "Event" = {}
        hint: "Hint" = {}

        result = before_send(event, hint)

        assert result is not None
        self.assertNotIn("fingerprint", result)


class BreadcrumbTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()

        # `set_sentry_breadcrumb_ring_size` is what `setup_sentry` calls; it is
        # process-wide, so put it back afterwards.
        self.addCleanup(
            setattr,
            logcontext,
            "_sentry_breadcrumb_ring_size",
            logcontext._sentry_breadcrumb_ring_size,
        )
        logcontext.set_sentry_breadcrumb_ring_size(3)

    def _make_crumb(
        self, message: str, category: str = "synapse.handlers.sync"
    ) -> "Breadcrumb":
        return {"category": category, "message": message}

    def test_child_contexts_share_the_ring(self) -> None:
        """A request's children (including database threads) share its ring."""
        with LoggingContext(
            name="GET-42", server_name=SERVER_NAME, request=make_context_request()
        ) as parent:
            with LoggingContext(
                name="child", server_name=SERVER_NAME, parent_context=parent
            ) as child:
                self.assertIs(child.sentry_breadcrumbs, parent.sentry_breadcrumbs)

    def test_sibling_contexts_have_separate_rings(self) -> None:
        """Two unrelated requests keep their breadcrumbs apart."""
        with LoggingContext(name="GET-1", server_name=SERVER_NAME) as first:
            pass
        with LoggingContext(name="GET-2", server_name=SERVER_NAME) as second:
            pass

        self.assertIsNot(first.sentry_breadcrumbs, second.sentry_breadcrumbs)

    def test_crumbs_go_to_the_current_context(self) -> None:
        """Crumbs are kept on the logcontext, not handed back to the SDK."""
        with LoggingContext(name="GET-1", server_name=SERVER_NAME) as first:
            self.assertIsNone(before_breadcrumb(self._make_crumb("one"), {}))
        with LoggingContext(name="GET-2", server_name=SERVER_NAME) as second:
            self.assertIsNone(before_breadcrumb(self._make_crumb("two"), {}))

        assert first.sentry_breadcrumbs is not None
        assert second.sentry_breadcrumbs is not None
        self.assertEqual(
            [crumb["message"] for crumb in first.sentry_breadcrumbs], ["one"]
        )
        self.assertEqual(
            [crumb["message"] for crumb in second.sentry_breadcrumbs], ["two"]
        )

    def test_crumb_is_stripped_and_serialised(self) -> None:
        """A kept crumb has the logcontext attributes off it and is JSON-ready."""
        crumb: "Breadcrumb" = {
            "category": "synapse.handlers.sync",
            "message": "hello",
            "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "data": {
                "ip_address": "1.2.3.4",
                "requester": "@alice:example.com",
                "url": "/_matrix/client/v3/sync?since=s1",
                "something_useful": 1,
            },
        }

        with LoggingContext(name="GET-1", server_name=SERVER_NAME) as context:
            self.assertIsNone(before_breadcrumb(crumb, {}))

        assert context.sentry_breadcrumbs is not None
        (kept,) = context.sentry_breadcrumbs
        self.assertEqual(kept["data"], {"something_useful": 1})
        # `json.dumps` is what the transport does with the event, and it is given no
        # `default=`, so a `datetime` left in the ring would drop the whole event.
        self.assertEqual(
            json.loads(json.dumps(kept)),
            {
                "category": "synapse.handlers.sync",
                "message": "hello",
                "timestamp": "2026-01-01T00:00:00.000000Z",
                "data": {"something_useful": 1},
            },
        )

    def test_ring_is_bounded(self) -> None:
        """Only the most recent crumbs are kept."""
        with LoggingContext(name="GET-1", server_name=SERVER_NAME) as context:
            for i in range(5):
                before_breadcrumb(self._make_crumb(str(i)), {})

        assert context.sentry_breadcrumbs is not None
        self.assertEqual(
            [crumb["message"] for crumb in context.sentry_breadcrumbs], ["2", "3", "4"]
        )

    def test_access_log_crumbs_are_dropped(self) -> None:
        """The access log repeats other requests' paths, so it is not kept."""
        with LoggingContext(name="GET-1", server_name=SERVER_NAME) as context:
            for category in ("synapse.access.http.8008", "synapse.access.https.8448"):
                self.assertIsNone(
                    before_breadcrumb(self._make_crumb("nope", category=category), {})
                )

        assert context.sentry_breadcrumbs is not None
        self.assertEqual(list(context.sentry_breadcrumbs), [])

    def test_sentinel_context_passes_crumbs_through(self) -> None:
        """Outside a logcontext, the SDK keeps its own ring."""
        crumb = self._make_crumb("one")
        self.assertIs(before_breadcrumb(crumb, {}), crumb)

    def test_before_send_attaches_the_ring(self) -> None:
        """The event carries this request's crumbs, not the SDK's shared ring."""
        event: "Event" = {"breadcrumbs": {"values": [{"message": "shared"}]}}
        hint: "Hint" = {}

        with LoggingContext(
            name="GET-42", server_name=SERVER_NAME, request=make_context_request()
        ):
            before_breadcrumb(self._make_crumb("one"), {})
            before_breadcrumb(self._make_crumb("two"), {})

            result = before_send(event, hint)

        assert result is not None
        breadcrumbs = result["breadcrumbs"]
        assert isinstance(breadcrumbs, dict)
        self.assertEqual(
            [crumb["message"] for crumb in breadcrumbs["values"]],
            ["one", "two"],
        )

    def test_no_breadcrumbs_when_disabled(self) -> None:
        """With no ring size set, the SDK's own breadcrumb handling is left alone."""
        logcontext._sentry_breadcrumb_ring_size = None
        event: "Event" = {}
        hint: "Hint" = {}

        with LoggingContext(name="GET-1", server_name=SERVER_NAME) as context:
            self.assertIsNone(context.sentry_breadcrumbs)
            crumb = self._make_crumb("one")
            self.assertIs(before_breadcrumb(crumb, {}), crumb)

            result = before_send(event, hint)

        assert result is not None
        self.assertNotIn("breadcrumbs", result)


class EndToEndTestCase(TestCase):
    """Drive `sentry_sdk` itself and assert on what reaches the transport.

    The SDK serialises and scrubs an event before it calls `before_send`, and
    JSON-encodes the envelope afterwards, so only a test which goes through the real
    client can see what leaves the process.

    `LoggingIntegration` patches `logging.Logger.callHandlers` process-wide and the SDK
    never undoes it; once the client below is dropped the patch finds no integration on
    the current client and does nothing.
    """

    def test_error_logged_during_a_request(self) -> None:
        """The event carries the request's log lines and none of its identifiers."""
        envelopes: list[Envelope] = []

        class CapturingTransport(Transport):
            def capture_envelope(self, envelope: Envelope) -> None:
                envelopes.append(envelope)

        client = sentry_sdk.Client(
            **sentry_sdk_options(
                dsn="https://key@example.invalid/1",
                environment="test",
                max_breadcrumbs=10,
            ),
            transport=CapturingTransport(),
            # The SDK's other default integrations reach outside this test (`sys.argv`,
            # `sys.excepthook`, an `atexit` hook); the logging one is what turns log
            # lines into events and breadcrumbs.
            default_integrations=False,
            integrations=[LoggingIntegration()],
        )
        self.addCleanup(client.close)

        self.addCleanup(
            setattr,
            logcontext,
            "_sentry_breadcrumb_ring_size",
            logcontext._sentry_breadcrumb_ring_size,
        )
        logcontext.set_sentry_breadcrumb_ring_size(10)

        logger = logging.getLogger("synapse.handlers.sync")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.INFO)

        with sentry_sdk.new_scope() as scope:
            # A forked scope drops the client again at the end of the test, where
            # `sentry_sdk.init` would install it on the global scope.
            scope.set_client(client)

            with LoggingContext(
                name="GET-42", server_name=SERVER_NAME, request=make_context_request()
            ):
                logger.info("Fetched messages for %s", "!room:example.com")
                logger.error("Everything is broken")

        (envelope,) = envelopes
        # `Envelope.serialize` is the JSON encoding the transport does; a value it
        # chokes on drops the event, logging only "Internal error in sentry_sdk".
        body = envelope.serialize()

        event = envelope.get_event()
        assert event is not None
        breadcrumbs = event["breadcrumbs"]
        assert isinstance(breadcrumbs, dict)
        self.assertEqual(
            [crumb["message"] for crumb in breadcrumbs["values"]],
            ["Fetched messages for !room:example.com"],
        )
        self.assertEqual(event["user"], {"id": pseudonym("@alice:example.com")})

        self.assertNotIn(b"1.2.3.4", body)
        self.assertNotIn(b"@alice:example.com", body)
        self.assertNotIn(b"from=s1", body)
