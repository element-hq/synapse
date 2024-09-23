import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from twisted.web.server import Request

from synapse.http.server import HttpServer
from synapse.replication.http._base import ReplicationEndpoint
from synapse.types import JsonDict, JsonMapping

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class ReplicationAddedDelayedEventRestServlet(ReplicationEndpoint):
    """Handle a delayed event being added by another worker.

    Request format:

        POST /_synapse/replication/delayed_event_added/

        {}
    """

    NAME = "added_delayed_event"
    PATH_ARGS = ()
    CACHE = False

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.handler = hs.get_delayed_events_handler()

    @staticmethod
    async def _serialize_payload(next_send_ts: int) -> JsonDict:  # type: ignore[override]
        return {"next_send_ts": next_send_ts}

    async def _handle_request(  # type: ignore[override]
        self, request: Request, content: JsonDict
    ) -> Tuple[int, Dict[str, Optional[JsonMapping]]]:
        self.handler.on_added(int(content["next_send_ts"]))

        return 200, {}


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    ReplicationAddedDelayedEventRestServlet(hs).register(http_server)
