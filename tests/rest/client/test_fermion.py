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

from unittest.mock import AsyncMock

from signedjson.key import get_verify_key
from signedjson.sign import verify_signed_json

from twisted.test.proto_helpers import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import fermion, login
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.unittest import FederatingHomeserverTestCase

ROOM = "/_matrix/client/unstable/org.matrix.experimental.fermion.v1/rooms/!r:test"
FED_ROOM = (
    "/_matrix/federation/unstable/org.matrix.experimental.fermion.v1/rooms/!r:test"
)


class FermionArbiterTestCase(FederatingHomeserverTestCase):
    """The arbiter orders the commits members report and signs rulings;
    a member's server relays to the room's arbiter server."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        fermion.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {"fermion_arbiter_enabled": True})
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)
        self.hs = hs
        self.register_user("a", "pass")
        self.tok = self.login("a", "pass")

    def submit(
        self,
        base: str,
        epoch: str,
        event_id: str,
        interval: int = 0,
        arbiter: str = "test",
    ) -> JsonDict:
        channel = self.make_request(
            "POST",
            ROOM + "/commits",
            content={
                "arbiter": arbiter,
                "base": base,
                "epoch": epoch,
                "event_id": event_id,
                "epoch_interval_ms": interval,
            },
            access_token=self.tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body

    def rulings(self, since: int = 0) -> list[JsonDict]:
        channel = self.make_request(
            "GET", ROOM + f"/rulings?arbiter=test&from={since}", access_token=self.tok
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        return channel.json_body["rulings"]

    def test_sequencer_accepts_the_first_commit_on_the_head(self) -> None:
        # The first submission seeds the head with its base.
        first = self.submit("e0", "e1", "$c1")
        self.assertTrue(first["accepted"])
        self.assertEqual(first["ruling"]["canonical"], "e1")
        self.assertEqual(first["ruling"]["commits"], ["$c1"])
        self.assertEqual(first["ruling"]["index"], 0)
        verify_signed_json(
            first["ruling"], self.hs.hostname, get_verify_key(self.hs.signing_key)
        )

        # A concurrent commit on e0 loses, and learns the head from the ruling.
        loser = self.submit("e0", "e1x", "$c1x")
        self.assertFalse(loser["accepted"])
        self.assertEqual(loser["ruling"]["canonical"], "e1")
        self.assertEqual(loser["ruling"]["index"], 0)

        # Building on the head wins again.
        second = self.submit("e1", "e2", "$c2")
        self.assertTrue(second["accepted"])
        self.assertEqual(second["ruling"]["canonical"], "e2")
        self.assertEqual(second["ruling"]["commits"], ["$c2"])
        self.assertEqual([r["index"] for r in self.rulings()], [0, 1])
        self.assertEqual([r["index"] for r in self.rulings(since=1)], [1])

    def test_interval_batches_commits_into_one_ruling(self) -> None:
        self.assertEqual(self.submit("e0", "e1", "$c1", interval=5000)["ruling"], None)
        self.assertEqual(self.submit("e1", "e2", "$c2", interval=5000)["ruling"], None)
        fork = self.submit("e1", "e2x", "$c2x", interval=5000)
        self.assertFalse(fork["accepted"])
        self.assertEqual(self.rulings(), [])
        self.reactor.advance(5)
        (ruling,) = self.rulings()
        self.assertEqual(ruling["canonical"], "e2")
        self.assertEqual(ruling["commits"], ["$c1", "$c2"])
        # The loser rebases: a resolution on the head is canonical.
        self.assertTrue(self.submit("e2", "e3", "$c3", interval=5000)["accepted"])
        self.reactor.advance(5)
        self.assertEqual(self.rulings(since=1)[0]["commits"], ["$c3"])

    def test_over_federation(self) -> None:
        # Another server's member reports to us, the arbiter.
        channel = self.make_signed_federation_request(
            "PUT",
            FED_ROOM + "/commits",
            content={
                "base": "e0",
                "epoch": "e1",
                "event_id": "$c1",
                "sender": "@b:other",
            },
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertTrue(channel.json_body["accepted"])
        channel = self.make_signed_federation_request(
            "GET", FED_ROOM + "/rulings?from=0"
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body["rulings"][0]["commits"], ["$c1"])

        # Our member reports to another server's arbiter: relayed, with the
        # sender filled in.
        client = self.hs.get_federation_http_client()
        client.put_json = AsyncMock(return_value={"accepted": True, "ruling": None})  # type: ignore[method-assign]
        self.assertTrue(self.submit("x0", "x1", "$d1", arbiter="other")["accepted"])
        destination, path = client.put_json.call_args.args
        self.assertEqual(destination, "other")
        self.assertEqual(path, FED_ROOM + "/commits")
        body = client.put_json.call_args.kwargs["data"]
        self.assertEqual(body["sender"], "@a:test")
        self.assertNotIn("arbiter", body)

    def test_bad_submissions(self) -> None:
        for content in (
            {"base": "e0"},
            {"arbiter": "test", "base": "e0"},
            {"arbiter": "test", "base": "e0", "epoch": "e1", "event_id": 1},
        ):
            channel = self.make_request(
                "POST", ROOM + "/commits", content=content, access_token=self.tok
            )
            self.assertEqual(channel.code, 400, content)
        channel = self.make_request("POST", ROOM + "/commits", content={})
        self.assertEqual(channel.code, 401)
        self.assertEqual(self.rulings(), [])
