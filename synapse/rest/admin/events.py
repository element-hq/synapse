from http import HTTPStatus
from typing import TYPE_CHECKING

from synapse.api.errors import NotFoundError
from synapse.events.utils import (
    SerializeEventConfig,
    format_event_raw,
    serialize_event,
)
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.admin import admin_patterns
from synapse.rest.admin._base import assert_user_is_admin
from synapse.storage.databases.main.events_worker import EventRedactBehaviour
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class EventRestServlet(RestServlet):
    """
    Get an event that is known to the homeserver.
    The requester must have administrator access in Synapse.

    GET /_synapse/admin/v1/fetch_event/<event_id>
    returns:
        200 OK with event json if the event is known to the homeserver. Otherwise raises
        a NotFound error.

    Args:
        event_id: the id of the requested event.
    Returns:
        JSON blob of the event
    """

    PATTERNS = admin_patterns("/fetch_event/(?P<event_id>[^/]*)$")

    def __init__(self, hs: "HomeServer"):
        self._auth = hs.get_auth()
        self._store = hs.get_datastores().main
        self._clock = hs.get_clock()

    async def on_GET(
        self, request: SynapseRequest, event_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self._auth.get_user_by_req(request)
        await assert_user_is_admin(self._auth, requester)

        event = await self._store.get_event(
            event_id,
            EventRedactBehaviour.as_is,
            allow_none=True,
        )

        if event is None:
            raise NotFoundError("Event not found")

        config = SerializeEventConfig(
            as_client_event=False,
            event_format=format_event_raw,
            requester=requester,
            only_event_fields=None,
            include_stripped_room_state=True,
            include_admin_metadata=True,
        )
        res = {"event": serialize_event(event, self._clock.time_msec(), config=config)}

        return HTTPStatus.OK, res
