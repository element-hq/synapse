from http import HTTPStatus
from typing import TYPE_CHECKING, Tuple

from synapse.api.errors import NotFoundError
from synapse.http.servlet import RestServlet
from synapse.http.site import SynapseRequest
from synapse.rest.admin import admin_patterns, assert_requester_is_admin
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

    async def on_GET(
        self, request: SynapseRequest, event_id: str
    ) -> Tuple[int, JsonDict]:
        await assert_requester_is_admin(self._auth, request)

        event = await self._store.get_event(
            event_id, EventRedactBehaviour.as_is, allow_none=True, allow_rejected=True
        )
        if event is None:
            raise NotFoundError("Event not found")

        res = {"event_id": event.event_id, "event": event.get_dict()}

        return HTTPStatus.OK, res
