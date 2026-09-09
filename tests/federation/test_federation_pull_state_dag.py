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

import logging
import urllib.parse
from http import HTTPStatus
from typing import Callable, TypeVar
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import (
    EventContentFields,
    EventTypes,
    JoinRules,
    Membership,
)
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.federation.transport.client import SendJoinResponse
from synapse.http.matrixfederationclient import ByteParser
from synapse.http.types import QueryParams
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.test_utils.event_builders import make_test_event

logger = logging.getLogger(__name__)

ROOM_VERSION = RoomVersions.MSC4242v12

T = TypeVar("T")


class FederationPullStateDagTestCase(unittest.FederatingHomeserverTestCase):
    """Tests for receiving pulled events in a remote MSC4242 State DAG room.

    A room is first joined over federation (the same way as the join tests), which leaves
    a real, fully-persisted state DAG on the local homeserver. Events are then fed through
    the real inbound pull path (`_process_pulled_event`), with the remote server's
    /get_missing_events responses supplied by the test (by mocking the federation HTTP
    client). This exercises the state DAG walk, outlier persistence, state/auth
    calculation and the soft-fail check for real.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self._federation_http_client = Mock()
        return self.setup_test_homeserver(
            federation_http_client=self._federation_http_client
        )

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4242_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.store = self.hs.get_datastores().main
        self.state_storage_controller = hs.get_storage_controllers().state
        self.local_user_id = self.register_user("user1", "pass")
        self.local_user_tok = self.login("user1", "pass")
        self.remote_creator_user_id = f"@remote-creator:{self.OTHER_SERVER_NAME}"
        self._depth = 0

    def _remote_event(
        self,
        room_id: str | None,
        event_type: str,
        state_key: str,
        content: JsonDict,
        prev_state_events: list[str],
        sender: str | None = None,
    ) -> EventBase:
        """Build a state event signed by the remote server."""
        self._depth += 1
        event_dict: JsonDict = {
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "sender": sender or self.remote_creator_user_id,
            "depth": self._depth,
            "origin_server_ts": self._depth,
            "prev_state_events": prev_state_events,
            "prev_events": prev_state_events,
        }
        if room_id is not None:
            event_dict["room_id"] = room_id

        return make_test_event(
            self.add_hashes_and_signatures_from_other_server(
                event_dict, room_version=ROOM_VERSION
            ),
            room_version=ROOM_VERSION,
        )

    def _build_public_room(
        self, extra_state_events: list[JsonDict] | None = None
    ) -> tuple[str, list[EventBase]]:
        """Build the state DAG of a public room on the remote server.

        Args:
            extra_state_events: `_remote_event` kwargs for extra state events to chain on
                to the end of the state DAG.
        Returns:
            The room ID and its state DAG, in causal order.
        """
        create_event = self._remote_event(
            None,
            EventTypes.Create,
            "",
            {EventContentFields.ROOM_VERSION: ROOM_VERSION.identifier},
            prev_state_events=[],
        )
        room_id = create_event.room_id
        state_dag = [create_event]

        def append(**kwargs: object) -> None:
            state_dag.append(
                self._remote_event(
                    room_id,
                    prev_state_events=[state_dag[-1].event_id],
                    **kwargs,  # type: ignore[arg-type]
                )
            )

        append(
            event_type=EventTypes.Member,
            state_key=self.remote_creator_user_id,
            content={"membership": Membership.JOIN},
        )
        append(
            event_type=EventTypes.JoinRules,
            state_key="",
            content={"join_rule": JoinRules.PUBLIC},
        )
        for kwargs in extra_state_events or []:
            append(**kwargs)

        return room_id, state_dag

    def _join(
        self,
        room_id: str,
        state_dag: list[EventBase],
        join_prev_state_events: list[str],
    ) -> str:
        """Join `room_id` via the real client-server /join API.

        The remote server's /make_join and /send_join HTTP responses are mocked so that
        the local homeserver's real federation join code runs against the given state
        DAG. `join_prev_state_events` is the state DAG the join event points at.

        Returns the local user's join event ID.
        """
        join_template = self._remote_event(
            room_id,
            EventTypes.Member,
            self.local_user_id,
            {"membership": Membership.JOIN},
            prev_state_events=join_prev_state_events,
            sender=self.local_user_id,
        )

        make_join_path = "/_matrix/federation/v1/make_join/" + "/".join(
            urllib.parse.quote_plus(part) for part in (room_id, self.local_user_id)
        )

        async def _get_json(
            destination: str,
            path: str,
            args: QueryParams | None = None,
            retry_on_dns_fail: bool = True,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: ByteParser[T] | None = None,
        ) -> JsonDict | T:
            if path == make_join_path:
                return {
                    "event": join_template.get_pdu_json(),
                    "room_version": ROOM_VERSION.identifier,
                }
            raise NotImplementedError(f"unmocked get_json for {path}")

        send_join_prefix = (
            f"/_matrix/federation/v2/send_join/{urllib.parse.quote_plus(room_id)}/"
        )

        async def _put_json(
            destination: str,
            path: str,
            args: QueryParams | None = None,
            data: JsonDict | None = None,
            json_data_callback: Callable[[], JsonDict] | None = None,
            long_retries: bool = False,
            timeout: int | None = None,
            ignore_backoff: bool = False,
            backoff_on_404: bool = False,
            try_trailing_slash_on_400: bool = False,
            parser: ByteParser[T] | None = None,
            backoff_on_all_error_codes: bool = False,
        ) -> JsonDict | T | SendJoinResponse:
            if path.startswith(send_join_prefix) and data is not None:
                # As the remote server, sign the join event the local server built.
                signed_join = make_test_event(
                    self.add_hashes_and_signatures_from_other_server(
                        data, room_version=ROOM_VERSION
                    ),
                    room_version=ROOM_VERSION,
                )
                return SendJoinResponse(
                    auth_events=[],
                    state=[],
                    state_dag=list(state_dag),
                    event_dict=signed_join.get_pdu_json(),
                    event=signed_join,
                    members_omitted=False,
                )
            raise NotImplementedError(f"unmocked put_json for {path}")

        self._federation_http_client.get_json.side_effect = _get_json
        self._federation_http_client.put_json.side_effect = _put_json

        # Join via the client-server API, telling it which server hosts the room. This
        # is a real remote join: everything below the mocked HTTP transport runs.
        channel = self.make_request(
            "POST",
            f"/_matrix/client/v3/join/{urllib.parse.quote(room_id)}"
            f"?server_name={self.OTHER_SERVER_NAME}",
            content={},
            access_token=self.local_user_tok,
        )
        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)

        _, join_event_id = self.get_success(
            self.store.get_local_current_membership_for_user_in_room(
                self.local_user_id, room_id
            )
        )
        assert join_event_id is not None
        return join_event_id

    def _process_pulled_event(self, event: EventBase) -> None:
        """Feed `event` through the real inbound pull path."""
        self.get_success(
            self.hs.get_federation_event_handler()._process_pulled_event(
                self.OTHER_SERVER_NAME, event, backfilled=False
            )
        )

    def test_pulled_event_fills_in_missing_state_dag(self) -> None:
        """A pulled event whose `prev_state_events` we don't have causes us to walk the
        state DAG over federation, persist the missing events, and calculate the state
        (and auth) before the event ourselves."""
        room_id, state_dag = self._build_public_room()
        join_event_id = self._join(room_id, state_dag, [state_dag[-1].event_id])

        # The remote server adds a room name (which we will be missing), then a topic
        # chained on to it. We only receive the topic, so we have to fetch the name.
        missing_name = self._remote_event(
            room_id,
            EventTypes.Name,
            "",
            {"name": "fetched via the state DAG"},
            prev_state_events=[join_event_id],
        )
        pulled_topic = self._remote_event(
            room_id,
            EventTypes.Topic,
            "",
            {"topic": "pulled"},
            prev_state_events=[missing_name.event_id],
        )

        async def _post_json(
            destination: str,
            path: str,
            data: JsonDict | None = None,
            timeout: int | None = None,
            **kwargs: object,
        ) -> JsonDict:
            if "/get_missing_events/" in path:
                return {"events": [missing_name.get_pdu_json()]}
            raise NotImplementedError(f"unmocked post_json for {path}")

        self._federation_http_client.post_json.side_effect = _post_json

        self._process_pulled_event(pulled_topic)

        # Both the pulled event and the state DAG event we were missing are now persisted.
        self.assertIsNotNone(
            self.get_success(
                self.store.get_event(missing_name.event_id, allow_none=True)
            )
        )
        persisted = self.get_success(
            self.store.get_event(pulled_topic.event_id, allow_none=True)
        )
        assert persisted is not None
        self.assertFalse(persisted.internal_metadata.is_outlier())

        # The state before the topic was calculated from the fetched state DAG, so the
        # room name we had to fetch is part of the state at the topic event.
        state = self.get_success(
            self.state_storage_controller.get_state_ids_for_event(pulled_topic.event_id)
        )
        self.assertEqual(state[(EventTypes.Name, "")], missing_name.event_id)
        self.assertEqual(state[(EventTypes.Topic, "")], pulled_topic.event_id)

    def test_pulled_event_is_soft_failed_against_the_current_state_dag(self) -> None:
        """A pulled event that passes auth at its own place in the state DAG but fails
        against the current state (calculated from the state DAG extremities) is
        soft-failed."""
        bob = f"@bob:{self.OTHER_SERVER_NAME}"
        room_id, state_dag = self._build_public_room(
            extra_state_events=[
                {
                    "event_type": EventTypes.Member,
                    "state_key": bob,
                    "content": {"membership": Membership.JOIN},
                    "sender": bob,
                }
            ]
        )
        join_event_id = self._join(room_id, state_dag, [state_dag[-1].event_id])

        # The creator bans bob, so bob is not in the room in the current state.
        ban = self._remote_event(
            room_id,
            EventTypes.Member,
            bob,
            {"membership": Membership.BAN},
            prev_state_events=[join_event_id],
        )
        self._process_pulled_event(ban)

        # A membership event from bob forked off *before* the ban: it was valid where it
        # sits in the DAG (bob was joined), but bob is banned in the current state.
        forked = self._remote_event(
            room_id,
            EventTypes.Member,
            bob,
            {"membership": Membership.JOIN},
            prev_state_events=[join_event_id],
            sender=bob,
        )
        self._process_pulled_event(forked)

        persisted = self.get_success(
            self.store.get_event(forked.event_id, allow_none=True)
        )
        assert persisted is not None
        # It is accepted (not rejected) but soft-failed, so it doesn't change the current
        # state: bob stays banned.
        self.assertFalse(persisted.internal_metadata.is_outlier())
        self.assertTrue(persisted.internal_metadata.soft_failed)
        state = self.get_success(
            self.state_storage_controller.get_current_state_ids(room_id)
        )
        current_bob_membership = self.get_success(
            self.store.get_event(state[(EventTypes.Member, bob)])
        )
        self.assertEqual(current_bob_membership.membership, Membership.BAN)
