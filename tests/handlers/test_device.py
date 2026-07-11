#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
# Copyright 2016 OpenMarket Ltd
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

from unittest import mock
from unittest.mock import AsyncMock, Mock, patch

import signedjson.key
from parameterized import parameterized
from signedjson.types import SigningKey

from twisted.internet import defer
from twisted.internet.defer import ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes, JoinRules, RoomEncryptionAlgorithms
from synapse.api.errors import NotFoundError, SynapseError
from synapse.api.room_versions import RoomVersions
from synapse.appservice import ApplicationService
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.events import EventBase, make_event_from_dict
from synapse.federation.federation_client import SendJoinResult
from synapse.federation.transport.client import (
    StateRequestResponse,
    TransportLayerClient,
)
from synapse.federation.units import Transaction
from synapse.handlers.device import MAX_DEVICE_DISPLAY_NAME_LEN, DeviceWriterHandler
from synapse.rest import admin
from synapse.rest.client import devices, login, register, room
from synapse.server import HomeServer
from synapse.storage.databases.main.appservice import _make_exclusive_regex
from synapse.types import (
    JsonDict,
    StateMap,
    UserID,
    create_requester,
    get_domain_from_id,
)
from synapse.util.clock import Clock
from synapse.util.duration import Duration
from synapse.util.task_scheduler import TaskScheduler

from tests import unittest
from tests.unittest import override_config

user1 = "@boris:aaa"
user2 = "@theresa:bbb"


class DeviceTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.appservice_api = mock.AsyncMock()
        hs = self.setup_test_homeserver(
            "server",
            application_service_api=self.appservice_api,
        )
        handler = hs.get_device_handler()
        assert isinstance(handler, DeviceWriterHandler)
        self.handler = handler
        self.store = hs.get_datastores().main
        self.device_message_handler = hs.get_device_message_handler()
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # These tests assume that it starts 1000 seconds in.
        self.reactor.advance(1000)

    def test_device_is_created_with_invalid_name(self) -> None:
        self.get_failure(
            self.handler.check_device_registered(
                user_id="@boris:foo",
                device_id="foo",
                initial_device_display_name="a" * (MAX_DEVICE_DISPLAY_NAME_LEN + 1),
            ),
            SynapseError,
        )

    def test_device_is_created_if_doesnt_exist(self) -> None:
        res = self.get_success(
            self.handler.check_device_registered(
                user_id="@boris:foo",
                device_id="fco",
                initial_device_display_name="display name",
            )
        )
        self.assertEqual(res, "fco")

        dev = self.get_success(self.handler.store.get_device("@boris:foo", "fco"))
        assert dev is not None
        self.assertEqual(dev["display_name"], "display name")

    def test_device_is_preserved_if_exists(self) -> None:
        res1 = self.get_success(
            self.handler.check_device_registered(
                user_id="@boris:foo",
                device_id="fco",
                initial_device_display_name="display name",
            )
        )
        self.assertEqual(res1, "fco")

        res2 = self.get_success(
            self.handler.check_device_registered(
                user_id="@boris:foo",
                device_id="fco",
                initial_device_display_name="new display name",
            )
        )
        self.assertEqual(res2, "fco")

        dev = self.get_success(self.handler.store.get_device("@boris:foo", "fco"))
        assert dev is not None
        self.assertEqual(dev["display_name"], "display name")

    def test_device_id_is_made_up_if_unspecified(self) -> None:
        device_id = self.get_success(
            self.handler.check_device_registered(
                user_id="@theresa:foo",
                device_id=None,
                initial_device_display_name="display",
            )
        )

        dev = self.get_success(self.handler.store.get_device("@theresa:foo", device_id))
        assert dev is not None
        self.assertEqual(dev["display_name"], "display")

    def test_get_devices_by_user(self) -> None:
        self._record_users()

        res = self.get_success(self.handler.get_devices_by_user(user1))

        self.assertEqual(3, len(res))
        device_map = {d["device_id"]: d for d in res}
        self.assertLessEqual(
            {
                "user_id": user1,
                "device_id": "xyz",
                "display_name": "display 0",
                "last_seen_ip": None,
                "last_seen_ts": None,
            }.items(),
            device_map["xyz"].items(),
        )
        self.assertLessEqual(
            {
                "user_id": user1,
                "device_id": "fco",
                "display_name": "display 1",
                "last_seen_ip": "ip1",
                "last_seen_ts": 1000000,
            }.items(),
            device_map["fco"].items(),
        )
        self.assertLessEqual(
            {
                "user_id": user1,
                "device_id": "abc",
                "display_name": "display 2",
                "last_seen_ip": "ip3",
                "last_seen_ts": 3000000,
            }.items(),
            device_map["abc"].items(),
        )

    def test_get_device(self) -> None:
        self._record_users()

        res = self.get_success(self.handler.get_device(user1, "abc"))
        self.assertLessEqual(
            {
                "user_id": user1,
                "device_id": "abc",
                "display_name": "display 2",
                "last_seen_ip": "ip3",
                "last_seen_ts": 3000000,
            }.items(),
            res.items(),
        )

    def test_delete_device(self) -> None:
        self._record_users()

        # delete the device
        self.get_success(self.handler.delete_devices(user1, ["abc"]))

        # check the device was deleted
        self.get_failure(self.handler.get_device(user1, "abc"), NotFoundError)

        # we'd like to check the access token was invalidated, but that's a
        # bit of a PITA.

    def test_delete_device_and_device_inbox(self) -> None:
        self._record_users()

        # add an device_inbox
        self.get_success(
            self.store.db_pool.simple_insert(
                "device_inbox",
                {
                    "user_id": user1,
                    "device_id": "abc",
                    "stream_id": 1,
                    "message_json": "{}",
                },
            )
        )

        # delete the device
        self.get_success(self.handler.delete_devices(user1, ["abc"]))

        # check that the device_inbox was deleted
        res = self.get_success(
            self.store.db_pool.simple_select_one(
                table="device_inbox",
                keyvalues={"user_id": user1, "device_id": "abc"},
                retcols=("user_id", "device_id"),
                allow_none=True,
                desc="get_device_id_from_device_inbox",
            )
        )
        self.assertIsNone(res)

    def test_delete_device_and_big_device_inbox(self) -> None:
        """Check that deleting a big device inbox is staged and batched asynchronously."""
        DEVICE_ID = "abc"
        sender = "@sender:" + self.hs.hostname
        receiver = "@receiver:" + self.hs.hostname
        self._record_user(sender, DEVICE_ID, DEVICE_ID)
        self._record_user(receiver, DEVICE_ID, DEVICE_ID)

        # queue a bunch of messages in the inbox
        requester = create_requester(sender, device_id=DEVICE_ID)
        for i in range(DeviceWriterHandler.DEVICE_MSGS_DELETE_BATCH_LIMIT + 10):
            self.get_success(
                self.device_message_handler.send_device_message(
                    requester, "message_type", {receiver: {"*": {"val": i}}}
                )
            )

        # delete the device
        self.get_success(self.handler.delete_devices(receiver, [DEVICE_ID]))

        # messages should be deleted up to DEVICE_MSGS_DELETE_BATCH_LIMIT straight away
        res = self.get_success(
            self.store.db_pool.simple_select_list(
                table="device_inbox",
                keyvalues={"user_id": receiver},
                retcols=("user_id", "device_id", "stream_id"),
                desc="get_device_id_from_device_inbox",
            )
        )
        self.assertEqual(10, len(res))

        # wait for the task scheduler to do a second delete pass
        self.reactor.advance(TaskScheduler.SCHEDULE_INTERVAL.as_secs())

        # remaining messages should now be deleted
        res = self.get_success(
            self.store.db_pool.simple_select_list(
                table="device_inbox",
                keyvalues={"user_id": receiver},
                retcols=("user_id", "device_id", "stream_id"),
                desc="get_device_id_from_device_inbox",
            )
        )
        self.assertEqual(0, len(res))

    def test_update_device(self) -> None:
        self._record_users()

        update = {"display_name": "new display"}
        self.get_success(self.handler.update_device(user1, "abc", update))

        res = self.get_success(self.handler.get_device(user1, "abc"))
        self.assertEqual(res["display_name"], "new display")

    def test_update_device_too_long_display_name(self) -> None:
        """Update a device with a display name that is invalid (too long)."""
        self._record_users()

        # Request to update a device display name with a new value that is longer than allowed.
        update = {"display_name": "a" * (MAX_DEVICE_DISPLAY_NAME_LEN + 1)}
        self.get_failure(
            self.handler.update_device(user1, "abc", update),
            SynapseError,
        )

        # Ensure the display name was not updated.
        res = self.get_success(self.handler.get_device(user1, "abc"))
        self.assertEqual(res["display_name"], "display 2")

    def test_update_unknown_device(self) -> None:
        update = {"display_name": "new_display"}
        self.get_failure(
            self.handler.update_device("user_id", "unknown_device_id", update),
            NotFoundError,
        )

    def _record_users(self) -> None:
        # check this works for both devices which have a recorded client_ip,
        # and those which don't.
        self._record_user(user1, "xyz", "display 0")
        self._record_user(user1, "fco", "display 1", "token1", "ip1")
        self._record_user(user1, "abc", "display 2", "token2", "ip2")
        self._record_user(user1, "abc", "display 2", "token3", "ip3")

        self._record_user(user2, "def", "dispkay", "token4", "ip4")

        self.reactor.advance(10000)

    def _record_user(
        self,
        user_id: str,
        device_id: str,
        display_name: str,
        access_token: str | None = None,
        ip: str | None = None,
    ) -> None:
        device_id = self.get_success(
            self.handler.check_device_registered(
                user_id=user_id,
                device_id=device_id,
                initial_device_display_name=display_name,
            )
        )

        if access_token is not None and ip is not None:
            self.get_success(
                self.store.insert_client_ip(
                    user_id, access_token, ip, "user_agent", device_id
                )
            )
            self.reactor.advance(1000)

    @override_config({"experimental_features": {"msc3984_appservice_key_query": True}})
    def test_on_federation_query_user_devices_appservice(self) -> None:
        """Test that querying of appservices for keys overrides responses from the database."""
        local_user = "@boris:" + self.hs.hostname
        device_1 = "abc"
        device_2 = "def"
        device_3 = "ghi"

        # There are 3 devices:
        #
        # 1. One which is uploaded to the homeserver.
        # 2. One which is uploaded to the homeserver, but a newer copy is returned
        #     by the appservice.
        # 3. One which is only returned by the appservice.
        device_key_1: JsonDict = {
            "user_id": local_user,
            "device_id": device_1,
            "algorithms": [
                "m.olm.curve25519-aes-sha2",
                RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2,
            ],
            "keys": {
                "ed25519:abc": "base64+ed25519+key",
                "curve25519:abc": "base64+curve25519+key",
            },
            "signatures": {local_user: {"ed25519:abc": "base64+signature"}},
        }
        device_key_2a: JsonDict = {
            "user_id": local_user,
            "device_id": device_2,
            "algorithms": [
                "m.olm.curve25519-aes-sha2",
                RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2,
            ],
            "keys": {
                "ed25519:def": "base64+ed25519+key",
                "curve25519:def": "base64+curve25519+key",
            },
            "signatures": {local_user: {"ed25519:def": "base64+signature"}},
        }

        device_key_2b: JsonDict = {
            "user_id": local_user,
            "device_id": device_2,
            "algorithms": [
                "m.olm.curve25519-aes-sha2",
                RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2,
            ],
            # The device ID is the same (above), but the keys are different.
            "keys": {
                "ed25519:xyz": "base64+ed25519+key",
                "curve25519:xyz": "base64+curve25519+key",
            },
            "signatures": {local_user: {"ed25519:xyz": "base64+signature"}},
        }
        device_key_3: JsonDict = {
            "user_id": local_user,
            "device_id": device_3,
            "algorithms": [
                "m.olm.curve25519-aes-sha2",
                RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2,
            ],
            "keys": {
                "ed25519:jkl": "base64+ed25519+key",
                "curve25519:jkl": "base64+curve25519+key",
            },
            "signatures": {local_user: {"ed25519:jkl": "base64+signature"}},
        }

        # Upload keys for devices 1 & 2a.
        e2e_keys_handler = self.hs.get_e2e_keys_handler()
        self.get_success(
            e2e_keys_handler.upload_keys_for_user(
                local_user, device_1, {"device_keys": device_key_1}
            )
        )
        self.get_success(
            e2e_keys_handler.upload_keys_for_user(
                local_user, device_2, {"device_keys": device_key_2a}
            )
        )

        # Inject an appservice interested in this user.
        appservice = ApplicationService(
            token="i_am_an_app_service",
            id="1234",
            namespaces={"users": [{"regex": r"@boris:.+", "exclusive": True}]},
            # Note: this user does not have to match the regex above
            sender=UserID.from_string("@as_main:test"),
        )
        self.hs.get_datastores().main.services_cache = [appservice]
        self.hs.get_datastores().main.exclusive_user_regex = _make_exclusive_regex(
            [appservice]
        )

        # Setup a response.
        self.appservice_api.query_keys.return_value = {
            "device_keys": {
                local_user: {device_2: device_key_2b, device_3: device_key_3}
            }
        }

        # Request all devices.
        res = self.get_success(
            self.handler.on_federation_query_user_devices(local_user)
        )
        self.assertIn("devices", res)
        res_devices = res["devices"]
        for device in res_devices:
            device["keys"].pop("unsigned", None)
        self.assertEqual(
            res_devices,
            [
                {"device_id": device_1, "keys": device_key_1},
                {"device_id": device_2, "keys": device_key_2b},
                {"device_id": device_3, "keys": device_key_3},
            ],
        )

    def test_delete_device_removes_refresh_tokens(self) -> None:
        """Deleting a device should also purge any refresh tokens for it."""
        self._record_users()

        self.get_success(
            self.store.add_refresh_token_to_user(
                user_id=user1,
                token="refresh_token",
                device_id="abc",
                expiry_ts=None,
                ultimate_session_expiry_ts=None,
            )
        )

        self.get_success(self.handler.delete_devices(user1, ["abc"]))

        remaining_refresh_token = self.get_success(
            self.store.db_pool.simple_select_one(
                table="refresh_tokens",
                keyvalues={"user_id": user1, "device_id": "abc"},
                retcols=("id",),
                desc="get_refresh_token_for_device",
                allow_none=True,
            )
        )
        self.assertIsNone(remaining_refresh_token)


class DeviceListExcludedRoomsTestCase(unittest.HomeserverTestCase):
    """Tests for `exclude_rooms_from_device_list_updates`.

    A configured room should be dropped from device-list update fan-out, but
    ONLY if it is unencrypted: excluding an encrypted room would break E2EE, so
    encrypted rooms in the exclusion set must keep being tracked.
    """

    servlets = [
        admin.register_servlets,
        login.register_servlets,
        register.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        handler = hs.get_device_handler()
        assert isinstance(handler, DeviceWriterHandler)
        self.handler = handler
        self.store = hs.get_datastores().main
        self.event_sources = hs.get_event_sources()

        self.user1 = self.register_user("user1", "pass")
        self.user1_tok = self.login(self.user1, "pass", device_id="user1device")
        self.user2 = self.register_user("user2", "pass")
        self.user2_tok = self.login(self.user2, "pass", device_id="user2device")

    def _create_shared_room(self, encrypted: bool = False) -> str:
        """Create a room owned by user1 that user2 also joins."""
        room_id = self.helper.create_room_as(self.user1, tok=self.user1_tok)
        if encrypted:
            self.helper.send_state(
                room_id,
                EventTypes.RoomEncryption,
                {"algorithm": RoomEncryptionAlgorithms.MEGOLM_V1_AES_SHA2},
                tok=self.user1_tok,
            )
        self.helper.join(room_id, self.user2, tok=self.user2_tok)
        return room_id

    def _changed_users_for_user2(self) -> set:
        """Have user1 change their device, then return the set of users that
        user2's device-list stream reports as changed since just before."""
        from_token = self.event_sources.get_current_token()

        self.get_success(self.handler.notify_device_update(self.user1, ["user1device"]))

        result = self.get_success(
            self.handler.get_user_ids_changed(self.user2, from_token)
        )
        return set(result.changed)

    def test_change_in_shared_room_is_notified(self) -> None:
        """Baseline: with no exclusion, a peer sharing a room IS notified."""
        self._create_shared_room()

        self.assertIn(self.user1, self._changed_users_for_user2())

    def test_excluded_unencrypted_room_is_not_notified(self) -> None:
        """A change in an excluded, unencrypted room does NOT notify a peer who
        only shares that room."""
        room_id = self._create_shared_room(encrypted=False)
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset({room_id})

        self.assertNotIn(self.user1, self._changed_users_for_user2())

    def test_non_excluded_room_still_notified(self) -> None:
        """A peer sharing a *non-excluded* room IS still notified, even when
        another (unshared) room is excluded."""
        room_id = self._create_shared_room(encrypted=False)
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset(
            {"!someotherroom:test"}
        )
        # Sanity check the shared room isn't the excluded one.
        self.assertNotEqual(room_id, "!someotherroom:test")

        self.assertIn(self.user1, self._changed_users_for_user2())

    def test_excluded_but_encrypted_room_is_still_notified(self) -> None:
        """If the excluded room is ENCRYPTED, the peer IS still notified: the
        guard preserves E2EE."""
        room_id = self._create_shared_room(encrypted=True)
        # Ensure the encryption state is visible via the bulk (cached) lookup
        # the handler actually uses (`get_room_encryption` is a stub).
        self.assertIsNotNone(
            self.get_success(self.store.bulk_get_room_encryption({room_id})).get(
                room_id
            )
        )
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset({room_id})

        self.assertIn(self.user1, self._changed_users_for_user2())

    def _changed_via_classic_sync_fallback(self, joined_room_ids: set) -> set:
        """Run `generate_sync_entry_for_device_list` (the classic /sync path)
        with the too-old-token fallback in `get_device_changes_in_shared_rooms`
        forced, and return the reported changed users for user2."""
        from_token = self.event_sources.get_current_token()
        # Record a device change for user1 so they appear in the per-user
        # `device_lists_stream` that the fallback consults.
        self.get_success(self.handler.notify_device_update(self.user1, ["user1device"]))
        now_token = self.event_sources.get_current_token()

        # Force `get_device_list_changes_in_rooms` to report the token as too
        # old, dropping into the fallback code path.
        with patch.object(
            self.store,
            "get_device_list_changes_in_rooms",
            AsyncMock(return_value=None),
        ):
            result = self.get_success(
                self.handler.generate_sync_entry_for_device_list(
                    user_id=self.user2,
                    since_token=from_token,
                    now_token=now_token,
                    joined_room_ids=joined_room_ids,
                    newly_joined_rooms=set(),
                    newly_joined_or_invited_or_knocked_users=set(),
                    newly_left_rooms=set(),
                    newly_left_users=set(),
                )
            )
        return set(result.changed)

    def test_classic_sync_fallback_excludes_unencrypted_room(self) -> None:
        """The classic /sync path (`generate_sync_entry_for_device_list`) passes
        an *unfiltered* joined-rooms set, so the filter must be applied inside
        that method. On the too-old-token fallback a peer sharing only an
        excluded, unencrypted room must NOT be reported as changed."""
        room_id = self._create_shared_room(encrypted=False)

        # Sanity: without exclusion, the fallback DOES surface user1 (otherwise
        # the assertion below would pass vacuously).
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset()
        self.assertIn(self.user1, self._changed_via_classic_sync_fallback({room_id}))

        # With the room excluded (and unencrypted), user1 is filtered out.
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset({room_id})
        self.assertNotIn(self.user1, self._changed_via_classic_sync_fallback({room_id}))

    @override_config({"exclude_rooms_from_device_list_updates": ["!excluded:test"]})
    def test_config_populates_handler(self) -> None:
        """The config option is wired through to the handler attribute the
        filter reads. Guards against a mis-named config key or attribute, which
        the other tests (which set the attribute directly) would not catch."""
        self.assertEqual(
            self.handler._rooms_to_exclude_from_device_list_updates,
            frozenset({"!excluded:test"}),
        )

    def test_newly_joined_excluded_room_does_not_pull_in_members(self) -> None:
        """Joining an excluded, unencrypted room must not report its existing
        members as changed: otherwise the join alone would trigger a
        `/keys/query` re-fetch of every member."""
        # user1 owns the room; user2 has NOT joined it yet.
        excluded_room = self.helper.create_room_as(self.user1, tok=self.user1_tok)
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset(
            {excluded_room}
        )

        from_token = self.event_sources.get_current_token()
        self.helper.join(excluded_room, self.user2, tok=self.user2_tok)
        changed = set(
            self.get_success(
                self.handler.get_user_ids_changed(self.user2, from_token)
            ).changed
        )
        self.assertNotIn(self.user1, changed)

        # Control: joining a *non-excluded* room DOES pull the existing member
        # in, so the assertion above is not vacuous.
        other_room = self.helper.create_room_as(self.user1, tok=self.user1_tok)
        from_token = self.event_sources.get_current_token()
        self.helper.join(other_room, self.user2, tok=self.user2_tok)
        changed = set(
            self.get_success(
                self.handler.get_user_ids_changed(self.user2, from_token)
            ).changed
        )
        self.assertIn(self.user1, changed)

    def test_excluded_room_not_recorded_for_federation(self) -> None:
        """A device change in an excluded, unencrypted room is not recorded
        against that room in `device_lists_changes_in_room`. Federation pokes
        (and local room wake-ups) derive from those rows, so an unrecorded room
        is never federated to its servers."""
        room_id = self._create_shared_room(encrypted=False)

        def recorded_changes_in_room() -> set:
            from_token = self.event_sources.get_current_token()
            self.get_success(
                self.handler.notify_device_update(self.user1, ["user1device"])
            )
            now_token = self.event_sources.get_current_token()
            return set(
                self.get_success(
                    self.handler.get_device_changes_in_shared_rooms(
                        self.user2,
                        [room_id],
                        from_token=from_token,
                        now_token=now_token,
                    )
                )
            )

        # Control: without exclusion, user1's change IS recorded against the
        # room (so it would wake local streams and federate).
        self.assertIn(self.user1, recorded_changes_in_room())

        # With the room excluded (and unencrypted), the change is not recorded
        # against it, so nothing fans out to that room's members or servers.
        self.handler._rooms_to_exclude_from_device_list_updates = frozenset({room_id})
        self.assertNotIn(self.user1, recorded_changes_in_room())


class DehydrationTestCase(unittest.HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        register.register_servlets,
        devices.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver("server")
        handler = hs.get_device_handler()
        assert isinstance(handler, DeviceWriterHandler)
        self.handler = handler
        self.message_handler = hs.get_device_message_handler()
        self.registration = hs.get_registration_handler()
        self.auth = hs.get_auth()
        self.auth_handler = hs.get_auth_handler()
        self.store = hs.get_datastores().main
        return hs

    @unittest.override_config({"experimental_features": {"msc3814_enabled": True}})
    def test_dehydrate_v2_and_fetch_events(self) -> None:
        user_id = "@boris:server"

        self.get_success(self.store.register_user(user_id, "foobar"))

        # First check if we can store and fetch a dehydrated device
        stored_dehydrated_device_id = self.get_success(
            self.handler.store_dehydrated_device(
                user_id=user_id,
                device_id=None,
                device_data={"device_data": {"foo": "bar"}},
                initial_device_display_name="dehydrated device",
                keys_for_device={},
            )
        )

        device_info = self.get_success(
            self.handler.get_dehydrated_device(user_id=user_id)
        )
        assert device_info is not None
        retrieved_device_id, device_data = device_info
        self.assertEqual(retrieved_device_id, stored_dehydrated_device_id)
        self.assertEqual(device_data, {"device_data": {"foo": "bar"}})

        # Create a new login for the user
        device_id, access_token, _expiration_time, _refresh_token = self.get_success(
            self.registration.register_device(
                user_id=user_id,
                device_id=None,
                initial_display_name="new device",
            )
        )

        requester = create_requester(user_id, device_id=device_id)

        # Fetching messages for a non-existing device should return an error
        self.get_failure(
            self.message_handler.get_events_for_dehydrated_device(
                requester=requester,
                device_id="not the right device ID",
                since_token=None,
                limit=10,
            ),
            SynapseError,
        )

        # Send some messages to the dehydrated device
        for i in range(12):
            ensureDeferred(
                self.message_handler.send_device_message(
                    requester=requester,
                    message_type="test.message",
                    messages={
                        user_id: {stored_dehydrated_device_id: {"body": f"foo_{i}"}}
                    },
                )
            )
        self.pump()

        # Fetch the first batch of messages from the dehydrated device
        res = self.get_success(
            self.message_handler.get_events_for_dehydrated_device(
                requester=requester,
                device_id=stored_dehydrated_device_id,
                since_token=None,
                limit=10,
            )
        )

        self.assertTrue(res.limited)
        # This batch contains the first 10 events
        self.assertEqual(len(res.events), 10)
        self.assertEqual(res.events[0]["content"]["body"], "foo_0")
        self.assertEqual(res.events[1]["content"]["body"], "foo_1")

        # Fetch the first batch again, which should return the same messages as they
        # have not been deleted
        res = self.get_success(
            self.message_handler.get_events_for_dehydrated_device(
                requester=requester,
                device_id=stored_dehydrated_device_id,
                since_token=None,
                limit=10,
            )
        )
        self.assertTrue(res.limited)
        self.assertEqual(len(res.events), 10)
        self.assertEqual(res.events[0]["content"]["body"], "foo_0")
        self.assertEqual(res.events[7]["content"]["body"], "foo_7")

        # Fetch the next batch
        res = self.get_success(
            self.message_handler.get_events_for_dehydrated_device(
                requester=requester,
                device_id=stored_dehydrated_device_id,
                since_token=res.stream_id,
                limit=10,
            )
        )
        # This is the last batch
        self.assertFalse(res.limited)
        # This batch contains the last 2 events
        self.assertEqual(len(res.events), 2)
        self.assertEqual(res.events[0]["content"]["body"], "foo_10")
        self.assertEqual(res.events[1]["content"]["body"], "foo_11")


@patch("synapse.crypto.keyring.Keyring.process_request", AsyncMock(return_value=None))
class DeviceUnPartialStateTestCase(unittest.HomeserverTestCase):
    """Tests that local device list changes during partial state are sent to
    remote servers when the room un-partials."""

    servlets = [
        admin.register_servlets,
        login.register_servlets,
    ]

    # The two remote servers to fake
    REMOTE1_SERVER_NAME = "remote1"
    REMOTE1_SERVER_SIGNATURE_KEY = signedjson.key.generate_signing_key("test")
    REMOTE1_USER = f"@user:{REMOTE1_SERVER_NAME}"

    REMOTE2_SERVER_NAME = "remote2"
    REMOTE2_SERVER_SIGNATURE_KEY = signedjson.key.generate_signing_key("test")
    REMOTE2_USER = f"@user:{REMOTE2_SERVER_NAME}"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable federation so that get_device_updates_by_remote works.
        config["federation_sender_instances"] = ["master"]
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        # Mock the federation transport client to prevent actual network calls.
        self.federation_transport_client = AsyncMock(TransportLayerClient)

        self.federation_transport_client.send_transaction.return_value = {}

        hs = self.setup_test_homeserver(
            federation_transport_client=self.federation_transport_client,
        )
        handler = hs.get_device_handler()
        assert isinstance(handler, DeviceWriterHandler)
        self.device_handler = handler
        self.store = hs.get_datastores().main

        return hs

    def _build_public_room(self) -> StateMap[EventBase]:
        """Build a public room DAG that has REMOTE1 in it"""

        room_id = f"!room:{self.REMOTE1_SERVER_NAME}"
        room_version = RoomVersions.V10

        events: list[EventBase] = []

        # First we make the create event
        create_event_dict: JsonDict = {
            "auth_events": [],
            "content": {
                "creator": self.REMOTE1_USER,
                "room_version": room_version.identifier,
            },
            "depth": 0,
            "origin_server_ts": 0,
            "prev_events": [],
            "room_id": room_id,
            "sender": self.REMOTE1_USER,
            "state_key": "",
            "type": EventTypes.Create,
        }

        add_hashes_and_signatures(
            room_version,
            create_event_dict,
            self.REMOTE1_SERVER_NAME,
            self.REMOTE1_SERVER_SIGNATURE_KEY,
        )

        create_event = make_event_from_dict(create_event_dict, room_version)
        events.append(create_event)

        room_version = self.hs.config.server.default_room_version
        join_event_dict: JsonDict = {
            "auth_events": [
                create_event.event_id,
            ],
            "content": {"membership": "join"},
            "depth": 1,
            "origin_server_ts": 100,
            "prev_events": [create_event.event_id],
            "sender": self.REMOTE1_USER,
            "state_key": self.REMOTE1_USER,
            "room_id": room_id,
            "type": EventTypes.Member,
        }
        add_hashes_and_signatures(
            room_version,
            join_event_dict,
            self.hs.hostname,
            self.hs.signing_key,
        )
        join_event = make_event_from_dict(join_event_dict, room_version)
        events.append(join_event)

        # Then set the join rules to public
        join_rules_event_dict: JsonDict = {
            "auth_events": [create_event.event_id, join_event.event_id],
            "content": {"join_rule": JoinRules.PUBLIC},
            "depth": 2,
            "origin_server_ts": 200,
            "prev_events": [join_event.event_id],
            "room_id": room_id,
            "sender": self.REMOTE1_USER,
            "state_key": "",
            "type": EventTypes.JoinRules,
        }

        add_hashes_and_signatures(
            room_version,
            join_rules_event_dict,
            self.REMOTE1_SERVER_NAME,
            self.REMOTE1_SERVER_SIGNATURE_KEY,
        )
        join_rules_event = make_event_from_dict(join_rules_event_dict, room_version)
        events.append(join_rules_event)

        return {(event.type, event.state_key): event for event in events}

    def _build_signed_join_event(
        self,
        room_id: str,
        user: str,
        signing_key: SigningKey,
        state: StateMap[EventBase],
    ) -> EventBase:
        """Build a join event for the local user, signed by the local server."""

        latest_event = max(state.values(), key=lambda e: e.depth)

        room_version = self.hs.config.server.default_room_version
        join_event_dict: JsonDict = {
            "auth_events": [
                state[(EventTypes.Create, "")].event_id,
                state[(EventTypes.JoinRules, "")].event_id,
            ],
            "content": {"membership": "join"},
            "depth": latest_event.depth + 1,
            "origin_server_ts": latest_event.origin_server_ts + 100,
            "prev_events": [latest_event.event_id],
            "sender": user,
            "state_key": user,
            "room_id": room_id,
            "type": EventTypes.Member,
        }
        add_hashes_and_signatures(
            room_version,
            join_event_dict,
            get_domain_from_id(user),
            signing_key,
        )
        return make_event_from_dict(join_event_dict, room_version)

    @parameterized.expand([("not_pruned", False), ("pruned", True)])
    @patch(
        "synapse.storage.databases.main.devices.PRUNE_DEVICE_LISTS_CHANGES_IN_ROOM_AGE",
        Duration(minutes=1),
    )
    def test_local_device_changes_sent_to_new_servers_on_un_partial_state(
        self, _test_suffix: str, prune_device_lists_change_in_room: bool
    ) -> None:
        """When a room un-partials, local device list changes made during the
        partial state period should be sent to remote servers that were NOT
        known at the time of the partial join.

        We do this by creating a room with one remote server, partialling
        joining it, then receiving a join event from a second remote server. The
        second remote server should receive a device list update EDU for any
        local device changes that happened during the partial state period.

        We parameterize this test over whether during the unpartial process we
        prune the `device_list_changes_in_room` table, to check that the
        unpartial process correctly handles the case.
        """

        local_user = self.register_user("alice", "password")
        self.login("alice", "password")

        # Build the remote room's state events.
        room_state = self._build_public_room()

        # Before joining, we mock out the federation endpoints that are used
        # during the unpartial process, so that we can control when the
        # unpartial process completes.
        get_room_state_ids_deferred: defer.Deferred[JsonDict] = defer.Deferred()
        get_room_state_deferred: defer.Deferred[StateRequestResponse] = defer.Deferred()
        self.federation_transport_client.get_room_state_ids = Mock(
            side_effect=[get_room_state_ids_deferred]
        )
        self.federation_transport_client.get_room_state = Mock(
            side_effect=[get_room_state_deferred]
        )

        # Now make the local server partially join the room.
        room_id = room_state[(EventTypes.Create, "")].room_id
        room_version = room_state[(EventTypes.Create, "")].room_version

        local_join_event = self._build_signed_join_event(
            room_id, local_user, self.hs.signing_key, room_state
        )

        # Mock the federation client endpoints for the partial join.
        mock_make_membership_event = AsyncMock(
            return_value=(self.REMOTE1_SERVER_NAME, local_join_event, room_version)
        )
        mock_send_join = AsyncMock(
            return_value=SendJoinResult(
                local_join_event,
                self.REMOTE1_SERVER_NAME,
                state=list(room_state.values()),
                auth_chain=list(room_state.values()),
                partial_state=True,
                # Only REMOTE1_SERVER_NAME is known at join time.
                servers_in_room={self.REMOTE1_SERVER_NAME},
            )
        )

        fed_handler = self.hs.get_federation_handler()
        fed_client = self.hs.get_federation_client()
        with (
            patch.object(
                fed_client, "make_membership_event", mock_make_membership_event
            ),
            patch.object(fed_client, "send_join", mock_send_join),
        ):
            self.get_success(
                fed_handler.do_invite_join(
                    [self.REMOTE1_SERVER_NAME], room_id, local_user, {}
                )
            )

        # The room should now be in partial state.
        self.assertTrue(self.get_success(self.store.is_partial_state_room(room_id)))

        # A local device change happens while the room is in partial state.
        self.get_success(
            self.store.add_device_change_to_streams(
                local_user, ["NEW_DEVICE"], [room_id]
            )
        )

        if prune_device_lists_change_in_room:
            # Add a device change for another room, as we won't prune the most
            # recent change.
            self.get_success(
                self.store.add_device_change_to_streams(
                    "@other:user", ["device1"], ["!some:room"]
                )
            )

            # Now prune the device list changes for the room. This simulates the
            # case where the unpartial process prunes the
            # `device_list_changes_in_room` table before processing the device
            # list changes.
            self.reactor.advance(120)  # Advance past the pruning threshold
            self.get_success(self.store._prune_device_lists_changes_in_room())

            # Assert we actually pruned the device list changes for the room.
            room_ids = self.get_success(
                self.store.db_pool.simple_select_onecol(
                    table="device_lists_changes_in_room",
                    keyvalues={},
                    retcol="room_id",
                )
            )
            self.assertCountEqual(room_ids, ["!some:room"])

        # Join the second server
        new_state = dict(room_state)
        new_state[(EventTypes.Member, local_user)] = local_join_event
        join_event_2 = self._build_signed_join_event(
            room_id,
            self.REMOTE2_USER,
            self.REMOTE2_SERVER_SIGNATURE_KEY,
            new_state,
        )

        self.get_success(
            self.hs.get_federation_event_handler().on_receive_pdu(
                self.REMOTE2_SERVER_NAME, join_event_2
            )
        )

        # Some EDUs may get sent out immediately, such as presence updates.
        # However, we only care about the device list update EDU sent by the
        # unpartialling process. Let's wait a few seconds and reset the mock.
        self.reactor.advance(5)
        self.federation_transport_client.send_transaction.reset_mock()

        # We now unblock the unpartial processs by returning the room state and
        # state ids. This should trigger the device list update to be sent to
        # REMOTE2_SERVER_NAME.
        self.federation_transport_client.get_room_state_ids.assert_called_once_with(
            self.REMOTE1_SERVER_NAME,
            room_id,
            event_id=local_join_event.prev_event_ids()[0],
        )

        get_room_state_ids_deferred.callback(
            {
                "pdu_ids": [event.event_id for event in room_state.values()],
                "auth_event_ids": [],
            }
        )
        get_room_state_deferred.callback(
            StateRequestResponse(
                state=list(room_state.values()),
                auth_events=[],
            )
        )

        # The device list EDU isn't necessarily sent out immediately
        self.reactor.advance(30)

        # Check that only one transaction was sent, and that it contains the
        # device list update EDU for the new device to REMOTE2_SERVER_NAME.
        self.federation_transport_client.send_transaction.assert_called_once()
        args, _ = self.federation_transport_client.send_transaction.call_args
        transaction: Transaction = args[0]

        self.assertEqual(transaction.destination, self.REMOTE2_SERVER_NAME)
        self.assertEqual(len(transaction.edus), 1)

        edu = transaction.edus[0]
        self.assertEqual(edu["edu_type"], "m.device_list_update")
        self.assertEqual(edu["content"]["device_id"], "NEW_DEVICE")
