from http import HTTPStatus
from typing import Optional, Set

from synapse.rest import admin
from synapse.rest.client import directory, login, room
from synapse.types import JsonDict

from tests import unittest


class RoomListHandlerTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        directory.register_servlets,
    ]

    def _create_published_room(
        self, tok: str, extra_content: Optional[JsonDict] = None
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
        room_ids_in_test2_list: Set[str] = {
            entry["room_id"] for entry in room_list["chunk"]
        }

        room_list = self.get_success(
            self.hs.get_room_list_handler().get_local_public_room_list(
                limit=50, from_federation_origin="test3"
            )
        )
        room_ids_in_test3_list: Set[str] = {
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
