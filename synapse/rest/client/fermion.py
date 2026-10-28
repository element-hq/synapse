#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""The fermion arbiter: ERA's finality node for a photon room created on
this server (`docs/boson/PLAN.md` in matrix-rust-sdk).

The server never joins the room's MLS group and is never sent a commit; it
sees only epoch ids and event ids. Members report each commit they make
(`base`, the DMLS epoch it is built on, and `epoch`, the one it starts),
the server keeps the first commit it saw on its current head as canonical,
and it signs rulings naming the canonical extremity and the commits that
reached it. At `epoch_interval_ms` 0 the ruling comes back in the
response, so a committer can ask before fanning out and drop a losing
commit (vanilla MLS with a sequencer); above 0 rulings are minted that
long after the first unruled commit, and members merge their losing
branches onto the canonical one.

A member talks to its own homeserver (the client endpoints here, with its
access token), which answers itself if it is the room's arbiter and
otherwise relays over federation (the servlets in
`synapse.federation.transport.server.fermion`); so the arbiter knows the
server and user behind every submission, and a browser never needs to
reach a foreign server.

ponytail: in-memory, per room, bounded by an LRU; a restart forgets the
head and the next submission re-seeds it.
"""

import logging
from typing import TYPE_CHECKING

from signedjson.sign import sign_json

from synapse.api.errors import SynapseError
from synapse.api.urls import FEDERATION_UNSTABLE_PREFIX
from synapse.http import servlet
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    assert_params_in_dict,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict
from synapse.util.caches.lrucache import LruCache
from synapse.util.duration import Duration

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

PREFIX = "/org.matrix.experimental.fermion.v1/rooms/(?P<room_id>[^/]*)"
FEDERATION_PATH = (
    FEDERATION_UNSTABLE_PREFIX + "/org.matrix.experimental.fermion.v1/rooms/"
)

# Rulings kept per room for `GET …/rulings?from=`.
RULINGS_KEPT = 64
MAX_ROOMS = 10_000


class ArbiterRoom:
    """What the arbiter remembers of one room."""

    def __init__(self) -> None:
        # The canonical extremity: the DMLS epoch id commits must build on.
        # None until the first submission seeds it with its base.
        self.head: str | None = None
        # Event ids of the commits accepted since the last ruling.
        self.accepted: list[str] = []
        self.rulings: list[JsonDict] = []
        # When the first unruled commit arrived, if a ruling is due.
        self.pending_since: int | None = None


class Arbiter:
    def __init__(self, hs: "HomeServer"):
        self.clock = hs.get_clock()
        self.server_name = hs.hostname
        self.signing_key = hs.signing_key
        self.rooms: LruCache[str, ArbiterRoom] = LruCache(
            max_size=MAX_ROOMS,
            clock=self.clock,
            server_name=self.server_name,
            cache_name="fermion_arbiter_rooms",
        )

    def room(self, room_id: str) -> ArbiterRoom:
        room = self.rooms.get(room_id)
        if room is None:
            room = ArbiterRoom()
            self.rooms[room_id] = room
        return room

    def submit(self, room_id: str, body: JsonDict, origin: str) -> JsonDict:
        """Record a commit reported by `origin`; whether it is canonical,
        and the ruling if one is minted now."""
        assert_params_in_dict(body, ["base", "epoch", "event_id"])
        base, epoch, event_id = body["base"], body["epoch"], body["event_id"]
        if not all(isinstance(v, str) for v in (base, epoch, event_id)):
            raise SynapseError(400, "'base', 'epoch' and 'event_id' must be strings")
        interval = body.get("epoch_interval_ms", 0)
        if not isinstance(interval, int) or interval < 0:
            raise SynapseError(
                400, "'epoch_interval_ms' must be a non-negative integer"
            )
        room = self.room(room_id)
        if room.head is None:
            room.head = base
        accepted = base == room.head
        if accepted:
            room.head = epoch
            room.accepted.append(event_id)
        logger.info(
            "fermion arbiter: %s commit %s on %s in %s from %s",
            "accepted" if accepted else "rejected",
            event_id,
            base,
            room_id,
            origin,
        )
        if interval == 0:
            if accepted or not room.rulings:
                self.rule(room_id, room)
            return {"accepted": accepted, "ruling": room.rulings[-1]}
        if accepted and room.pending_since is None:
            room.pending_since = self.clock.time_msec()
            self.clock.call_later(
                Duration(milliseconds=interval), self.rule, room_id, room
            )
        return {"accepted": accepted, "ruling": None}

    def rule(self, room_id: str, room: ArbiterRoom) -> None:
        """Mint a ruling: the canonical extremity and the commits accepted
        since the last one, signed by this server."""
        ruling = {
            "room_id": room_id,
            "index": len(room.rulings),
            "canonical": room.head,
            "commits": room.accepted,
            "ts": self.clock.time_msec(),
        }
        room.rulings.append(sign_json(ruling, self.server_name, self.signing_key))
        del room.rulings[:-RULINGS_KEPT]
        room.accepted = []
        room.pending_since = None

    def rulings(self, room_id: str, since: int) -> list[JsonDict]:
        room = self.rooms.get(room_id)
        if room is None:
            return []
        return [r for r in room.rulings if r["index"] >= since]


class ArbiterCommitsServlet(servlet.RestServlet):
    """`POST …/rooms/{roomId}/commits` with `{arbiter, base, epoch, event_id,
    epoch_interval_ms}`: report a commit to the room's arbiter server."""

    PATTERNS = client_patterns(PREFIX + "/commits$", releases=(), unstable=True)
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer", arbiter: Arbiter):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.arbiter = arbiter
        self.federation = hs.get_federation_http_client()

    async def on_POST(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        body = parse_json_object_from_request(request)
        assert_params_in_dict(body, ["arbiter"])
        destination = body.pop("arbiter")
        if not isinstance(destination, str):
            raise SynapseError(400, "'arbiter' must be a server name")
        body["sender"] = requester.user.to_string()
        if destination == self.hs.hostname:
            return 200, self.arbiter.submit(room_id, body, self.hs.hostname)
        return 200, await self.federation.put_json(
            destination, FEDERATION_PATH + room_id + "/commits", data=body
        )


class ArbiterRulingsServlet(servlet.RestServlet):
    """`GET …/rooms/{roomId}/rulings?arbiter=&from=`: the room's rulings
    since index `from`."""

    PATTERNS = client_patterns(PREFIX + "/rulings$", releases=(), unstable=True)
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer", arbiter: Arbiter):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.arbiter = arbiter
        self.federation = hs.get_federation_http_client()

    async def on_GET(
        self, request: SynapseRequest, room_id: str
    ) -> tuple[int, JsonDict]:
        await self.auth.get_user_by_req(request)
        destination = parse_string(request, "arbiter", required=True)
        since = parse_integer(request, "from", default=0)
        if destination == self.hs.hostname:
            return 200, {"rulings": self.arbiter.rulings(room_id, since)}
        return 200, await self.federation.get_json(
            destination,
            FEDERATION_PATH + room_id + "/rulings",
            args={"from": str(since)},
        )


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    if not hs.config.experimental.fermion_arbiter_enabled:
        return
    arbiter = hs.get_fermion_arbiter()
    ArbiterCommitsServlet(hs, arbiter).register(http_server)
    ArbiterRulingsServlet(hs, arbiter).register(http_server)
