#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#
from typing import Callable, FrozenSet, List, Optional, Set
from unittest.mock import AsyncMock, Mock

from signedjson import key, sign
from signedjson.types import BaseKey, SigningKey

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import EduTypes, RoomEncryptionAlgorithms
from synapse.api.presence import UserPresenceState
from synapse.federation.sender.per_destination_queue import MAX_PRESENCE_STATES_PER_EDU
from synapse.federation.units import Transaction
from synapse.handlers.device import DeviceHandler
from synapse.rest import admin
from synapse.rest.client import login
from synapse.server import HomeServer
from synapse.types import JsonDict, ReadReceipt
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class FederationSenderReceiptsTestCases(HomeserverTestCase):
    """
    Test federation sending to update receipts.

    By default for test cases federation sending is disabled. This Test class has it
    re-enabled for the main process.
    """

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_transport_client = Mock(spec=["send_transaction"])
        self.federation_transport_client.send_transaction = AsyncMock()
        hs = self.setup_test_homeserver(
            federation_transport_client=self.federation_transport_client,
        )

        hs.get_storage_controllers().state.get_current_hosts_in_room = AsyncMock(  # type: ignore[method-assign]
            return_value={"test", "host2"}
        )

        hs.get_storage_controllers().state.get_current_hosts_in_room_or_partial_state_approximation = (  # type: ignore[method-assign]
            hs.get_storage_controllers().state.get_current_hosts_in_room
        )

        return hs

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["federation_sender_instances"] = None
        return config

    def test_send_receipts(self) -> None:
        mock_send_transaction = self.federation_transport_client.send_transaction
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()
        receipt = ReadReceipt(
            "room_id",
            "m.read",
            "user_id",
            ["event_id"],
            thread_id=None,
            data={"ts": 1234},
        )
        self.get_success(sender.send_read_receipt(receipt))

        self.pump()

        # expect a call to send_transaction
        mock_send_transaction.assert_called_once()
        json_cb = mock_send_transaction.call_args[0][1]
        data = json_cb()
        self.assertEqual(
            data["edus"],
            [
                {
                    "edu_type": EduTypes.RECEIPT,
                    "content": {
                        "room_id": {
                            "m.read": {
                                "user_id": {
                                    "event_ids": ["event_id"],
                                    "data": {"ts": 1234},
                                }
                            }
                        }
                    },
                }
            ],
        )

    def test_send_receipts_thread(self) -> None:
        mock_send_transaction = self.federation_transport_client.send_transaction
        mock_send_transaction.return_value = {}

        # Create receipts for:
        #
        # * The same room / user on multiple threads.
        # * A different user in the same room.
        sender = self.hs.get_federation_sender()
        # Hack so that we have a txn in-flight so we batch up read receipts
        # below
        sender.wake_destination("host2")
        for user, thread in (
            ("alice", None),
            ("alice", "thread"),
            ("bob", None),
            ("bob", "diff-thread"),
        ):
            receipt = ReadReceipt(
                "room_id",
                "m.read",
                user,
                ["event_id"],
                thread_id=thread,
                data={"ts": 1234},
            )
            defer.ensureDeferred(sender.send_read_receipt(receipt))

        self.pump()

        # expect a call to send_transaction with two EDUs to separate threads.
        mock_send_transaction.assert_called_once()
        json_cb = mock_send_transaction.call_args[0][1]
        data = json_cb()
        # Note that the ordering of the EDUs doesn't matter.
        self.assertCountEqual(
            data["edus"],
            [
                {
                    "edu_type": EduTypes.RECEIPT,
                    "content": {
                        "room_id": {
                            "m.read": {
                                "alice": {
                                    "event_ids": ["event_id"],
                                    "data": {"ts": 1234, "thread_id": "thread"},
                                },
                                "bob": {
                                    "event_ids": ["event_id"],
                                    "data": {"ts": 1234, "thread_id": "diff-thread"},
                                },
                            }
                        }
                    },
                },
                {
                    "edu_type": EduTypes.RECEIPT,
                    "content": {
                        "room_id": {
                            "m.read": {
                                "alice": {
                                    "event_ids": ["event_id"],
                                    "data": {"ts": 1234},
                                },
                                "bob": {
                                    "event_ids": ["event_id"],
                                    "data": {"ts": 1234},
                                },
                            }
                        }
                    },
                },
            ],
        )

    def test_send_receipts_with_backoff(self) -> None:
        """Send two receipts in quick succession; the second should be flushed, but
        only after 20ms"""
        mock_send_transaction = self.federation_transport_client.send_transaction
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()
        receipt = ReadReceipt(
            "room_id",
            "m.read",
            "user_id",
            ["event_id"],
            thread_id=None,
            data={"ts": 1234},
        )
        self.get_success(sender.send_read_receipt(receipt))

        self.pump()

        # expect a call to send_transaction
        mock_send_transaction.assert_called_once()
        json_cb = mock_send_transaction.call_args[0][1]
        data = json_cb()
        self.assertEqual(
            data["edus"],
            [
                {
                    "edu_type": EduTypes.RECEIPT,
                    "content": {
                        "room_id": {
                            "m.read": {
                                "user_id": {
                                    "event_ids": ["event_id"],
                                    "data": {"ts": 1234},
                                }
                            }
                        }
                    },
                }
            ],
        )
        mock_send_transaction.reset_mock()

        # send the second RR
        receipt = ReadReceipt(
            "room_id",
            "m.read",
            "user_id",
            ["other_id"],
            thread_id=None,
            data={"ts": 1234},
        )
        self.successResultOf(defer.ensureDeferred(sender.send_read_receipt(receipt)))
        self.pump()
        mock_send_transaction.assert_not_called()

        self.reactor.advance(19)
        mock_send_transaction.assert_not_called()

        self.reactor.advance(10)
        mock_send_transaction.assert_called_once()
        json_cb = mock_send_transaction.call_args[0][1]
        data = json_cb()
        self.assertEqual(
            data["edus"],
            [
                {
                    "edu_type": EduTypes.RECEIPT,
                    "content": {
                        "room_id": {
                            "m.read": {
                                "user_id": {
                                    "event_ids": ["other_id"],
                                    "data": {"ts": 1234},
                                }
                            }
                        }
                    },
                }
            ],
        )


class FederationSenderPresenceTestCases(HomeserverTestCase):
    """
    Test federation sending for presence updates.
    """

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_transport_client = Mock(spec=["send_transaction"])
        self.federation_transport_client.send_transaction = AsyncMock()
        hs = self.setup_test_homeserver(
            federation_transport_client=self.federation_transport_client,
        )

        return hs

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["federation_sender_instances"] = None
        return config

    def test_presence_simple(self) -> None:
        "Test that sending a single presence update works"

        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()
        self.get_success(
            sender.send_presence_to_destinations(
                [UserPresenceState.default("@user:test")],
                ["server"],
            )
        )

        self.pump()

        # expect a call to send_transaction
        mock_send_transaction.assert_awaited_once()

        json_cb = mock_send_transaction.call_args[0][1]
        data = json_cb()
        self.assertEqual(
            data["edus"],
            [
                {
                    "edu_type": EduTypes.PRESENCE,
                    "content": {
                        "push": [
                            {
                                "presence": "offline",
                                "user_id": "@user:test",
                            }
                        ]
                    },
                }
            ],
        )

    def test_presence_batched(self) -> None:
        """Test that sending lots of presence updates to a destination are
        batched, rather than having them all sent in one EDU."""

        mock_send_transaction: AsyncMock = (
            self.federation_transport_client.send_transaction
        )
        mock_send_transaction.return_value = {}

        sender = self.hs.get_federation_sender()

        # We now send lots of presence updates to force the federation sender to
        # batch the mup.
        number_presence_updates_to_send = MAX_PRESENCE_STATES_PER_EDU * 2
        self.get_success(
            sender.send_presence_to_destinations(
                [
                    UserPresenceState.default(f"@user{i}:test")
                    for i in range(number_presence_updates_to_send)
                ],
                ["server"],
            )
        )

        self.pump()

        # We should have seen at least one transcation be sent by now.
        mock_send_transaction.assert_called()

        # We don't want to specify exactly how the presence EDUs get sent out,
        # could be one per transaction or multiple per transaction. We just want
        # to assert that a) each presence EDU has bounded number of updates, and
        # b) that all updates get sent out.
        presence_edus = []
        for transaction_call in mock_send_transaction.call_args_list:
            json_cb = transaction_call[0][1]
            data = json_cb()

            for edu in data["edus"]:
                self.assertEqual(edu.get("edu_type"), EduTypes.PRESENCE)
                presence_edus.append(edu)

        # A set of all user presence we see, this should end up matching the
        # number we sent out above.
        seen_users: Set[str] = set()

        for edu in presence_edus:
            presence_states = edu["content"]["push"]

            # This is where we actually check that the number of presence
            # updates is bounded.
            self.assertLessEqual(len(presence_states), MAX_PRESENCE_STATES_PER_EDU)

            seen_users.update(p["user_id"] for p in presence_states)

        self.assertEqual(len(seen_users), number_presence_updates_to_send)


class FederationSenderDevicesTestCases(HomeserverTestCase):
    """
    Test federation sending to update devices.

    By default for test cases federation sending is disabled. This Test class has it
    re-enabled for the main process.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_transport_client = Mock(
            spec=["send_transaction", "query_user_devices"]
        )
        self.federation_transport_client.send_transaction = AsyncMock()
        self.federation_transport_client.query_user_devices = AsyncMock()
        return self.setup_test_homeserver(
            federation_transport_client=self.federation_transport_client,
        )

    def default_config(self) -> JsonDict:
        c = super().default_config()
        # Enable federation sending on the main process.
        c["federation_sender_instances"] = None
        return c

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        test_room_id = "!room:host1"

        # stub out `get_rooms_for_user` and `get_current_hosts_in_room` so that the
        # server thinks the user shares a room with `@user2:host2`
        def get_rooms_for_user(user_id: str) -> "defer.Deferred[FrozenSet[str]]":
            return defer.succeed(frozenset({test_room_id}))

        hs.get_datastores().main.get_rooms_for_user = get_rooms_for_user  # type: ignore[assignment]

        async def get_current_hosts_in_room(room_id: str) -> Set[str]:
            if room_id == test_room_id:
                return {"host2"}
            else:
                # TODO: We should fail the test when we encounter an unxpected room ID.
                # We can't just use `self.fail(...)` here because the app code is greedy
                # with `Exception` and will catch it before the test can see it.
                return set()

        hs.get_datastores().main.get_current_hosts_in_room = get_current_hosts_in_room  # type: ignore[assignment]

        device_handler = hs.get_device_handler()
        assert isinstance(device_handler, DeviceHandler)
        self.device_handler = device_handler

        # whenever send_transaction is called, record the edu data
        self.edus: List[JsonDict] = []
        self.federation_transport_client.send_transaction.side_effect = (
            self.record_transaction
        )

    async def record_transaction(
        self, txn: Transaction, json_cb: Optional[Callable[[], JsonDict]] = None
    ) -> JsonDict:
        assert json_cb is not None
        data = json_cb()
        self.edus.extend(data["edus"])
        return {}

    def test_send_device_updates(self) -> None:
        """Basic case: each device update should result in an EDU"""
        # create a device
        u1 = self.register_user("user", "pass")
        self.login(u1, "pass", device_id="D1")

        # expect one edu
        self.assertEqual(len(self.edus), 1)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D1", None)

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # a second call should produce no new device EDUs
        self.get_success(
            self.hs.get_federation_sender().send_device_messages(["host2"])
        )
        self.assertEqual(self.edus, [])

        # a second device
        self.login("user", "pass", device_id="D2")

        self.assertEqual(len(self.edus), 1)
        self.check_device_update_edu(self.edus.pop(0), u1, "D2", stream_id)

    def test_dont_send_device_updates_for_remote_users(self) -> None:
        """Check that we don't send device updates for remote users"""

        # Send the server a device list EDU for the other user, this will cause
        # it to try and resync the device lists.
        self.federation_transport_client.query_user_devices.return_value = {
            "stream_id": "1",
            "user_id": "@user2:host2",
            "devices": [{"device_id": "D1"}],
        }

        self.get_success(
            self.device_handler.device_list_updater.incoming_device_list_update(
                "host2",
                {
                    "user_id": "@user2:host2",
                    "device_id": "D1",
                    "stream_id": "1",
                    "prev_ids": [],
                },
            )
        )

        self.reactor.advance(1)

        # We shouldn't see an EDU for that update
        self.assertEqual(self.edus, [])

        # Check that we did successfully process the inbound EDU (otherwise this
        # test would pass if we failed to process the EDU)
        devices = self.get_success(
            self.hs.get_datastores().main.get_cached_devices_for_user("@user2:host2")
        )
        self.assertIn("D1", devices)

    def test_upload_signatures(self) -> None:
        """Uploading signatures on some devices should produce updates for that user"""

        e2e_handler = self.hs.get_e2e_keys_handler()

        # register two devices
        u1 = self.register_user("user", "pass")
        self.login(u1, "pass", device_id="D1")
        self.login(u1, "pass", device_id="D2")

        # expect two edus
        self.assertEqual(len(self.edus), 2)
        stream_id: Optional[int] = None
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D1", stream_id)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D2", stream_id)

        # upload signing keys for each device
        device1_signing_key = self.generate_and_upload_device_signing_key(u1, "D1")
        device2_signing_key = self.generate_and_upload_device_signing_key(u1, "D2")

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # expect two more edus
        self.assertEqual(len(self.edus), 2)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D1", stream_id)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D2", stream_id)

        # upload master key and self-signing key
        master_signing_key = generate_self_id_key()
        master_key = {
            "user_id": u1,
            "usage": ["master"],
            "keys": {key_id(master_signing_key): encode_pubkey(master_signing_key)},
        }

        # private key: HvQBbU+hc2Zr+JP1sE0XwBe1pfZZEYtJNPJLZJtS+F8
        selfsigning_signing_key = generate_self_id_key()
        selfsigning_key = {
            "user_id": u1,
            "usage": ["self_signing"],
            "keys": {
                key_id(selfsigning_signing_key): encode_pubkey(selfsigning_signing_key)
            },
        }
        sign.sign_json(selfsigning_key, u1, master_signing_key)

        cross_signing_keys = {
            "master_key": master_key,
            "self_signing_key": selfsigning_key,
        }

        self.get_success(
            e2e_handler.upload_signing_keys_for_user(u1, cross_signing_keys)
        )

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # expect signing key update edu
        self.assertEqual(len(self.edus), 2)
        self.assertEqual(self.edus.pop(0)["edu_type"], EduTypes.SIGNING_KEY_UPDATE)
        self.assertEqual(
            self.edus.pop(0)["edu_type"], EduTypes.UNSTABLE_SIGNING_KEY_UPDATE
        )

        # sign the devices
        d1_json = build_device_dict(u1, "D1", device1_signing_key)
        sign.sign_json(d1_json, u1, selfsigning_signing_key)
        d2_json = build_device_dict(u1, "D2", device2_signing_key)
        sign.sign_json(d2_json, u1, selfsigning_signing_key)

        ret = self.get_success(
            e2e_handler.upload_signatures_for_device_keys(
                u1,
                {u1: {"D1": d1_json, "D2": d2_json}},
            )
        )
        self.assertEqual(ret["failures"], {})

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # expect two edus, in one or two transactions. We don't know what order the
        # devices will be updated.
        self.assertEqual(len(self.edus), 2)
        stream_id = None  # FIXME: there is a discontinuity in the stream IDs: see https://github.com/matrix-org/synapse/issues/7142
        for edu in self.edus:
            self.assertEqual(edu["edu_type"], EduTypes.DEVICE_LIST_UPDATE)
            c = edu["content"]
            if stream_id is not None:
                self.assertEqual(c["prev_id"], [stream_id])  # type: ignore[unreachable]
                self.assertGreaterEqual(c["stream_id"], stream_id)
            stream_id = c["stream_id"]
        devices = {edu["content"]["device_id"] for edu in self.edus}
        self.assertEqual({"D1", "D2"}, devices)

    def test_delete_devices(self) -> None:
        """If devices are deleted, that should result in EDUs too"""

        # create devices
        u1 = self.register_user("user", "pass")
        self.login("user", "pass", device_id="D1")
        self.login("user", "pass", device_id="D2")
        self.login("user", "pass", device_id="D3")

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # expect three edus
        self.assertEqual(len(self.edus), 3)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D1", None)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D2", stream_id)
        stream_id = self.check_device_update_edu(self.edus.pop(0), u1, "D3", stream_id)

        # delete them again
        self.get_success(self.device_handler.delete_devices(u1, ["D1", "D2", "D3"]))

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # expect three edus, in an unknown order
        self.assertEqual(len(self.edus), 3)
        for edu in self.edus:
            self.assertEqual(edu["edu_type"], EduTypes.DEVICE_LIST_UPDATE)
            c = edu["content"]
            self.assertGreaterEqual(
                c.items(),
                {"user_id": u1, "prev_id": [stream_id], "deleted": True}.items(),
            )
            self.assertGreaterEqual(c["stream_id"], stream_id)
            stream_id = c["stream_id"]
        devices = {edu["content"]["device_id"] for edu in self.edus}
        self.assertEqual({"D1", "D2", "D3"}, devices)

    def test_unreachable_server(self) -> None:
        """If the destination server is unreachable, all the updates should get sent on
        recovery
        """
        mock_send_txn = self.federation_transport_client.send_transaction
        mock_send_txn.side_effect = AssertionError("fail")

        # create devices
        u1 = self.register_user("user", "pass")
        self.login("user", "pass", device_id="D1")
        self.login("user", "pass", device_id="D2")
        self.login("user", "pass", device_id="D3")

        # delete them again
        self.get_success(self.device_handler.delete_devices(u1, ["D1", "D2", "D3"]))

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        self.assertGreaterEqual(mock_send_txn.call_count, 4)

        # recover the server
        mock_send_txn.side_effect = self.record_transaction
        self.get_success(
            self.hs.get_federation_sender().send_device_messages(["host2"])
        )

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # for each device, there should be a single update
        self.assertEqual(len(self.edus), 3)
        stream_id: Optional[int] = None
        for edu in self.edus:
            self.assertEqual(edu["edu_type"], EduTypes.DEVICE_LIST_UPDATE)
            c = edu["content"]
            self.assertEqual(c["prev_id"], [stream_id] if stream_id is not None else [])
            if stream_id is not None:
                self.assertGreaterEqual(c["stream_id"], stream_id)
            stream_id = c["stream_id"]
        devices = {edu["content"]["device_id"] for edu in self.edus}
        self.assertEqual({"D1", "D2", "D3"}, devices)

    def test_prune_outbound_device_pokes1(self) -> None:
        """If a destination is unreachable, and the updates are pruned, we should get
        a single update.

        This case tests the behaviour when the server has never been reachable.
        """
        mock_send_txn = self.federation_transport_client.send_transaction
        mock_send_txn.side_effect = AssertionError("fail")

        # create devices
        u1 = self.register_user("user", "pass")
        self.login("user", "pass", device_id="D1")
        self.login("user", "pass", device_id="D2")
        self.login("user", "pass", device_id="D3")

        # delete them again
        self.get_success(self.device_handler.delete_devices(u1, ["D1", "D2", "D3"]))

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        self.assertGreaterEqual(mock_send_txn.call_count, 4)

        # run the prune job
        self.reactor.advance(10)
        self.get_success(
            self.hs.get_datastores().main._prune_old_outbound_device_pokes(prune_age=1)
        )

        # recover the server
        mock_send_txn.side_effect = self.record_transaction
        self.get_success(
            self.hs.get_federation_sender().send_device_messages(["host2"])
        )

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # there should be a single update for this user.
        self.assertEqual(len(self.edus), 1)
        edu = self.edus.pop(0)
        self.assertEqual(edu["edu_type"], EduTypes.DEVICE_LIST_UPDATE)
        c = edu["content"]

        # synapse uses an empty prev_id list to indicate "needs a full resync".
        self.assertEqual(c["prev_id"], [])

    def test_prune_outbound_device_pokes2(self) -> None:
        """If a destination is unreachable, and the updates are pruned, we should get
        a single update.

        This case tests the behaviour when the server was reachable, but then goes
        offline.
        """

        # create first device
        u1 = self.register_user("user", "pass")
        self.login("user", "pass", device_id="D1")

        # expect the update EDU
        self.assertEqual(len(self.edus), 1)
        self.check_device_update_edu(self.edus.pop(0), u1, "D1", None)

        # now the server goes offline
        mock_send_txn = self.federation_transport_client.send_transaction
        mock_send_txn.side_effect = AssertionError("fail")

        self.login("user", "pass", device_id="D2")
        self.login("user", "pass", device_id="D3")

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # delete them again
        self.get_success(self.device_handler.delete_devices(u1, ["D1", "D2", "D3"]))

        self.assertGreaterEqual(mock_send_txn.call_count, 3)

        # run the prune job
        self.reactor.advance(10)
        self.get_success(
            self.hs.get_datastores().main._prune_old_outbound_device_pokes(prune_age=1)
        )

        # recover the server
        mock_send_txn.side_effect = self.record_transaction
        self.get_success(
            self.hs.get_federation_sender().send_device_messages(["host2"])
        )

        # We queue up device list updates to be sent over federation, so we
        # advance to clear the queue.
        self.reactor.advance(1)

        # ... and we should get a single update for this user.
        self.assertEqual(len(self.edus), 1)
        edu = self.edus.pop(0)
        self.assertEqual(edu["edu_type"], EduTypes.DEVICE_LIST_UPDATE)
        c = edu["content"]

        # synapse uses an empty prev_id list to indicate "needs a full resync".
        self.assertEqual(c["prev_id"], [])

    def check_device_update_edu(
        self,
        edu: JsonDict,
        user_id: str,
        device_id: str,
        prev_stream_id: Optional[int],
    ) -> int:
        """Check that the given EDU is an update for the given device
        Returns the stream_id.
        """
        self.assertEqual(edu["edu_type"], EduTypes.DEVICE_LIST_UPDATE)
        content = edu["content"]

        expected = {
            "user_id": user_id,
            "device_id": device_id,
            "prev_id": [prev_stream_id] if prev_stream_id is not None else [],
        }

        self.assertLessEqual(expected.items(), content.items())
        if prev_stream_id is not None:
            self.assertGreaterEqual(content["stream_id"], prev_stream_id)
        return content["stream_id"]

    def check_signing_key_update_txn(
        self,
        txn: JsonDict,
    ) -> None:
        """Check that the txn has an EDU with a signing key update."""
        edus = txn["edus"]
        self.assertEqual(len(edus), 2)

    def generate_and_upload_device_signing_key(
        self, user_id: str, device_id: str
    ) -> SigningKey:
        """Generate a signing keypair for the given device, and upload it"""
        sk = key.generate_signing_key(device_id)

        device_dict = build_device_dict(user_id, device_id, sk)

        self.get_success(
            self.hs.get_e2e_keys_handler().upload_keys_for_user(
                user_id,
                device_id,
                {"device_keys": device_dict},
            )
        )
        return sk


def generate_self_id_key() -> SigningKey:
    """generate a signing key whose version is its public key

    ... as used by the cross-signing-keys.
    """
    k = key.generate_signing_key("x")
    k.version = encode_pubkey(k)
    return k


def key_id(k: BaseKey) -> str:
    return "%s:%s" % (k.alg, k.version)


def encode_pubkey(sk: SigningKey) -> str:
    """Encode the public key corresponding to the given signing key as base64"""
    return key.encode_verify_key_base64(key.get_verify_key(sk))


def build_device_dict(user_id: str, device_id: str, sk: SigningKey) -> JsonDict:
    """Build a dict representing the given device"""
    return {
        "user_id": user_id,
        "device_id": device_id,
        "algorithms": [
            "m.olm.curve25519-aes-sha2",
            RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2,
        ],
        "keys": {
            "curve25519:" + device_id: "curve25519+key",
            key_id(sk): encode_pubkey(sk),
        },
    }
