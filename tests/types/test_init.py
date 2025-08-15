from synapse.api.errors import SynapseError
from synapse.types import RoomID

from tests.unittest import TestCase


class RoomIDTestCase(TestCase):
    def test_can_create_msc4291_room_ids(self) -> None:
        valid_msc4291_room_id = "!31hneApxJ_1o-63DmFrpeqnkFfWppnzWso1JvH3ogLM"
        room_id = RoomID.from_string(valid_msc4291_room_id)
        self.assertEquals(RoomID.is_valid(valid_msc4291_room_id), True)
        self.assertEquals(
            room_id.to_string(),
            valid_msc4291_room_id,
        )
        self.assertEquals(room_id.id, "!31hneApxJ_1o-63DmFrpeqnkFfWppnzWso1JvH3ogLM")
        self.assertEquals(room_id.get_domain(), None)

    def test_cannot_create_invalid_msc4291_room_ids(self) -> None:
        invalid_room_ids = [
            "!wronglength",
            "!31hneApxJ_1o-63DmFrpeqnNOTurlsafeBASE64/gLM",
            "!",
            "!                                           ",
        ]
        for bad_room_id in invalid_room_ids:
            with self.assertRaises(SynapseError):
                RoomID.from_string(bad_room_id)
                if not RoomID.is_valid(bad_room_id):
                    raise SynapseError(400, "invalid")

    def test_cannot_create_invalid_legacy_room_ids(self) -> None:
        invalid_room_ids = [
            "!something:invalid$_chars.com",
        ]
        for bad_room_id in invalid_room_ids:
            with self.assertRaises(SynapseError):
                RoomID.from_string(bad_room_id)
                if not RoomID.is_valid(bad_room_id):
                    raise SynapseError(400, "invalid")

    def test_can_create_valid_legacy_room_ids(self) -> None:
        valid_room_ids = [
            "!foo:example.com",
            "!foo:example.com:8448",
            "!💩💩💩:example.com",
        ]
        for room_id_str in valid_room_ids:
            room_id = RoomID.from_string(room_id_str)
            self.assertEquals(RoomID.is_valid(room_id_str), True)
            self.assertIsNotNone(room_id.get_domain())
