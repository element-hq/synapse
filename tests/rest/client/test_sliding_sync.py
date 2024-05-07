from synapse.api.constants import EduTypes
from synapse.rest import admin
from synapse.rest.client import login, sendtodevice, sync
from synapse.types import JsonDict

from tests.unittest import HomeserverTestCase, override_config


class SendToDeviceTestCase(HomeserverTestCase):
    servlets = [
        admin.register_servlets,
        login.register_servlets,
        sendtodevice.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def test_user_to_user(self) -> None:
        """A to-device message from one user to another should get delivered"""

        user1 = self.register_user("u1", "pass")
        user1_tok = self.login("u1", "pass", "d1")

        user2 = self.register_user("u2", "pass")
        user2_tok = self.login("u2", "pass", "d2")

        # send the message
        test_msg = {"foo": "bar"}
        chan = self.make_request(
            "PUT",
            "/_matrix/client/r0/sendToDevice/m.test/1234",
            content={"messages": {user2: {"d2": test_msg}}},
            access_token=user1_tok,
        )
        self.assertEqual(chan.code, 200, chan.result)

        # check it appears
        channel = self.make_request("GET", "/sync", access_token=user2_tok)
        self.assertEqual(channel.code, 200, channel.result)
        expected_result = {
            "events": [
                {
                    "sender": user1,
                    "type": "m.test",
                    "content": test_msg,
                }
            ]
        }
        self.assertEqual(channel.json_body["to_device"], expected_result)

        # it should re-appear if we do another sync because the to-device message is not
        # deleted until we acknowledge it by sending a `?since=...` parameter in the
        # next sync request corresponding to the `next_batch` value from the response.
        channel = self.make_request(
            "GET",
            "/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body["to_device"], expected_result)

        # it should *not* appear if we do an incremental sync
        sync_token = channel.json_body["next_batch"]
        channel = self.make_request(
            "GET",
            f"/_matrix/client/unstable/org.matrix.msc3575/sync/e2ee?since={sync_token}",
            access_token=user2_tok,
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(channel.json_body.get("to_device", {}).get("events", []), [])
