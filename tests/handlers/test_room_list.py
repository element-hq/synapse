from http import HTTPStatus
from unittest.mock import AsyncMock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import PublicRoomsFilterFields
from synapse.api.errors import Codes, SynapseError
from synapse.rest import admin
from synapse.rest.client import directory, login, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests import unittest
from tests.utils import default_config


class RoomListHandlerTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        directory.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.mock_policy_handler = AsyncMock()

        # Other tests ideally ensure that the handler respects the configuration correctly.
        # We're interested in testing that the handler is called, not that it's configured.
        async def assert_neutral_search_query(query: str) -> None:
            if query == "test_intentional_failure":
                raise SynapseError(
                    HTTPStatus.BAD_REQUEST, "mocked policy fail", Codes.FORBIDDEN
                )
            self.assertEqual(query, "test_search_term")

        self.mock_policy_handler.assert_neutral_search_query = (
            assert_neutral_search_query
        )
        hs = self.setup_test_homeserver(
            server_policy_handler=self.mock_policy_handler,
        )
        return hs

    def _create_published_room(
        self, tok: str, extra_content: JsonDict | None = None
    ) -> str:
        room_id = self.helper.create_room_as(tok=tok, extra_content=extra_content)
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/directory/list/room/{room_id}?access_token={tok}",
            content={
                "visibility": "public",
            },
        )
        assert channel.code == HTTPStatus.OK, f"couldn't publish room: {channel.result}"
        return room_id

    def default_config(self) -> JsonDict:
        config = default_config("test")
        config["room_list_publication_rules"] = [{"action": "allow"}]
        return config

    def test_acls_applied_to_room_directory_results(self) -> None:
        """
        Creates 3 rooms. Room 2 has an ACL that only permits the homeservers
        `test` and `test2` to access it.

        We then simulate `test2` and `test3` requesting the room directory and assert
        that `test3` does not see room 2, but `test2` sees all 3.
        """
        self.register_user("u1", "p1")
        u1tok = self.login("u1", "p1")
        room1 = self._create_published_room(u1tok)

        room2 = self._create_published_room(
            u1tok,
            extra_content={
                "initial_state": [
                    {
                        "type": "m.room.server_acl",
                        "content": {
                            "allow": ["test", "test2"],
                        },
                    }
                ]
            },
        )

        room3 = self._create_published_room(u1tok)

        room_list = self.get_success(
            self.hs.get_room_list_handler().get_local_public_room_list(
                limit=50, from_federation_origin="test2"
            )
        )
        room_ids_in_test2_list: set[str] = {
            entry["room_id"] for entry in room_list["chunk"]
        }

        room_list = self.get_success(
            self.hs.get_room_list_handler().get_local_public_room_list(
                limit=50, from_federation_origin="test3"
            )
        )
        room_ids_in_test3_list: set[str] = {
            entry["room_id"] for entry in room_list["chunk"]
        }

        self.assertEqual(
            room_ids_in_test2_list,
            {room1, room2, room3},
            "test2 should be able to see all 3 rooms",
        )
        self.assertEqual(
            room_ids_in_test3_list,
            {room1, room3},
            "test3 should be able to see only 2 rooms",
        )

    def test_policyserv_can_intercept_searches(self) -> None:
        """
        Tests that if a "safety policy" is configured, then that policy
        server is consulted when requests for a search term are made.

        This functionality doesn't apply when there's no search term.

        No rooms are required to test this - the expected output is an
        error to force zero results being returned.

        Typically, this functionality is used to intercept unsafe searches
        for rooms and instead "redirect" the caller to elsewhere. The redirect
        is done socially, not technically - the user is provided links to
        support resources they can access.
        """
        # Per docstring, quickly assert that no search means no policyserv call.
        # This works because our mock handler will assert that the search query
        # is a specific value - `None`/an empty string is not that value.
        self.get_success(
            self.hs.get_room_list_handler().get_local_public_room_list(
                search_filter=None,
            )
        )

        # Now test that "safe" search queries are passed through normally
        self.get_success(
            self.hs.get_room_list_handler().get_local_public_room_list(
                search_filter={
                    PublicRoomsFilterFields.GENERIC_SEARCH_TERM: "test_search_term",
                },
            )
        )

        # Finally, test that an "unsafe" search query is intercepted by policyserv
        err = self.get_failure(
            self.hs.get_room_list_handler().get_local_public_room_list(
                search_filter={
                    PublicRoomsFilterFields.GENERIC_SEARCH_TERM: "test_intentional_failure",
                },
            ),
            SynapseError,
        ).value
        self.assertEqual(err.code, HTTPStatus.BAD_REQUEST)
        self.assertEqual(err.errcode, Codes.FORBIDDEN)
