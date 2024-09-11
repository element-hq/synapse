"""Tests REST events for /delayed_events paths."""

from http import HTTPStatus

from tests.unittest import HomeserverTestCase

from synapse.rest.client import delayed_events

PATH_PREFIX = b"/_matrix/client/unstable/org.matrix.msc4140/delayed_events"


class DelayedEventsTestCase(HomeserverTestCase):
    """Tests getting and managing delayed events."""

    servlets = [delayed_events.register_servlets]

    user_id = "@sid1:red"

    def test_get_delayed_events(self) -> None:
        channel = self.make_request("GET", PATH_PREFIX)
        self.assertEqual(HTTPStatus.OK, channel.code, channel.result)
        self.assertEqual(
            [],
            channel.json_body.get("delayed_events"),
            channel.json_body,
        )
