#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

from typing import Any, Collection, List, Optional, Tuple
from unittest.mock import AsyncMock, Mock

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.auth.internal import InternalAuth
from synapse.api.constants import UserTypes
from synapse.api.errors import (
    CodeMessageException,
    Codes,
    ResourceLimitError,
    SynapseError,
)
from synapse.module_api import ModuleApi
from synapse.server import HomeServer
from synapse.spam_checker_api import RegistrationBehaviour
from synapse.types import (
    JsonDict,
    Requester,
    RoomAlias,
    RoomID,
    UserID,
    create_requester,
)
from synapse.util import Clock

from tests.unittest import override_config
from tests.utils import mock_getRawHeaders

from .. import unittest


class TestSpamChecker:
    def __init__(self, config: None, api: ModuleApi):
        api.register_spam_checker_callbacks(
            check_registration_for_spam=self.check_registration_for_spam,
        )

    @staticmethod
    def parse_config(config: JsonDict) -> None:
        return None

    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
        auth_provider_id: Optional[str],
    ) -> RegistrationBehaviour:
        return RegistrationBehaviour.ALLOW


class DenyAll(TestSpamChecker):
    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
        auth_provider_id: Optional[str],
    ) -> RegistrationBehaviour:
        return RegistrationBehaviour.DENY


class BanAll(TestSpamChecker):
    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
        auth_provider_id: Optional[str],
    ) -> RegistrationBehaviour:
        return RegistrationBehaviour.SHADOW_BAN


class BanBadIdPUser(TestSpamChecker):
    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
        auth_provider_id: Optional[str] = None,
    ) -> RegistrationBehaviour:
        # Reject any user coming from CAS and whose username contains profanity
        if auth_provider_id == "cas" and username and "flimflob" in username:
            return RegistrationBehaviour.DENY
        return RegistrationBehaviour.ALLOW


class TestLegacyRegistrationSpamChecker:
    def __init__(self, config: None, api: ModuleApi):
        pass

    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
    ) -> RegistrationBehaviour:
        return RegistrationBehaviour.ALLOW


class LegacyAllowAll(TestLegacyRegistrationSpamChecker):
    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
    ) -> RegistrationBehaviour:
        return RegistrationBehaviour.ALLOW


class LegacyDenyAll(TestLegacyRegistrationSpamChecker):
    async def check_registration_for_spam(
        self,
        email_threepid: Optional[dict],
        username: Optional[str],
        request_info: Collection[Tuple[str, str]],
    ) -> RegistrationBehaviour:
        return RegistrationBehaviour.DENY


class RegistrationTestCase(unittest.HomeserverTestCase):
    """Tests the RegistrationHandler."""

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs_config = self.default_config()

        # some of the tests rely on us having a user consent version
        hs_config.setdefault("user_consent", {}).update(
            {
                "version": "test_consent_version",
                "template_dir": ".",
            }
        )
        hs_config["max_mau_value"] = 50
        hs_config["limit_usage_by_mau"] = True

        # Don't attempt to reach out over federation.
        self.mock_federation_client = Mock()
        self.mock_federation_client.make_query.side_effect = CodeMessageException(
            500, ""
        )

        hs = self.setup_test_homeserver(
            config=hs_config, federation_client=self.mock_federation_client
        )

        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.handler = self.hs.get_registration_handler()
        self.store = self.hs.get_datastores().main
        self.lots_of_users = 100
        self.small_number_of_users = 1

        self.requester = create_requester("@requester:test")

    def test_user_is_created_and_logged_in_if_doesnt_exist(self) -> None:
        frank = UserID.from_string("@frank:test")
        user_id = frank.to_string()
        requester = create_requester(user_id)
        result_user_id, result_token = self.get_success(
            self.get_or_create_user(requester, frank.localpart, "Frankie")
        )
        self.assertEqual(result_user_id, user_id)
        self.assertIsInstance(result_token, str)
        self.assertGreater(len(result_token), 20)

    def test_if_user_exists(self) -> None:
        store = self.hs.get_datastores().main
        frank = UserID.from_string("@frank:test")
        self.get_success(
            store.register_user(user_id=frank.to_string(), password_hash=None)
        )
        local_part = frank.localpart
        user_id = frank.to_string()
        requester = create_requester(user_id)
        result_user_id, result_token = self.get_success(
            self.get_or_create_user(requester, local_part, None)
        )
        self.assertEqual(result_user_id, user_id)
        self.assertTrue(result_token is not None)

    @override_config({"limit_usage_by_mau": False})
    def test_mau_limits_when_disabled(self) -> None:
        # Ensure does not throw exception
        self.get_success(self.get_or_create_user(self.requester, "a", "display_name"))

    @override_config({"limit_usage_by_mau": True})
    def test_get_or_create_user_mau_not_blocked(self) -> None:
        self.store.count_monthly_users = AsyncMock(  # type: ignore[method-assign]
            return_value=self.hs.config.server.max_mau_value - 1
        )
        # Ensure does not throw exception
        self.get_success(self.get_or_create_user(self.requester, "c", "User"))

    @override_config({"limit_usage_by_mau": True})
    def test_get_or_create_user_mau_blocked(self) -> None:
        self.store.get_monthly_active_count = AsyncMock(return_value=self.lots_of_users)
        self.get_failure(
            self.get_or_create_user(self.requester, "b", "display_name"),
            ResourceLimitError,
        )

        self.store.get_monthly_active_count = AsyncMock(
            return_value=self.hs.config.server.max_mau_value
        )
        self.get_failure(
            self.get_or_create_user(self.requester, "b", "display_name"),
            ResourceLimitError,
        )

    @override_config({"limit_usage_by_mau": True})
    def test_register_mau_blocked(self) -> None:
        self.store.get_monthly_active_count = AsyncMock(return_value=self.lots_of_users)
        self.get_failure(
            self.handler.register_user(localpart="local_part"), ResourceLimitError
        )

        self.store.get_monthly_active_count = AsyncMock(
            return_value=self.hs.config.server.max_mau_value
        )
        self.get_failure(
            self.handler.register_user(localpart="local_part"), ResourceLimitError
        )

    @override_config(
        {"auto_join_rooms": ["#room:test"], "auto_join_rooms_for_guests": False}
    )
    def test_auto_join_rooms_for_guests(self) -> None:
        user_id = self.get_success(
            self.handler.register_user(localpart="jeff", make_guest=True),
        )
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)

    @override_config({"auto_join_rooms": ["#room:test"]})
    def test_auto_create_auto_join_rooms(self) -> None:
        room_alias_str = "#room:test"
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        self.assertTrue(room_id["room_id"] in rooms)
        self.assertEqual(len(rooms), 1)

    @override_config({"auto_join_rooms": []})
    def test_auto_create_auto_join_rooms_with_no_rooms(self) -> None:
        frank = UserID.from_string("@frank:test")
        user_id = self.get_success(self.handler.register_user(frank.localpart))
        self.assertEqual(user_id, frank.to_string())
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)

    @override_config({"auto_join_rooms": ["#room:another"]})
    def test_auto_create_auto_join_where_room_is_another_domain(self) -> None:
        frank = UserID.from_string("@frank:test")
        user_id = self.get_success(self.handler.register_user(frank.localpart))
        self.assertEqual(user_id, frank.to_string())
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)

    @override_config(
        {"auto_join_rooms": ["#room:test"], "autocreate_auto_join_rooms": False}
    )
    def test_auto_create_auto_join_where_auto_create_is_false(self) -> None:
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)

    @override_config({"auto_join_rooms": ["#room:test"]})
    def test_auto_create_auto_join_rooms_when_user_is_not_a_real_user(self) -> None:
        room_alias_str = "#room:test"
        self.store.is_real_user = AsyncMock(return_value=False)
        user_id = self.get_success(self.handler.register_user(localpart="support"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        self.get_failure(directory_handler.get_association(room_alias), SynapseError)

    @override_config({"auto_join_rooms": ["#room:test"]})
    def test_auto_create_auto_join_rooms_when_user_is_the_first_real_user(self) -> None:
        room_alias_str = "#room:test"

        self.store.count_real_users = AsyncMock(return_value=1)  # type: ignore[method-assign]
        self.store.is_real_user = AsyncMock(return_value=True)
        user_id = self.get_success(self.handler.register_user(localpart="real"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        self.assertTrue(room_id["room_id"] in rooms)
        self.assertEqual(len(rooms), 1)

    @override_config({"auto_join_rooms": ["#room:test"]})
    def test_auto_create_auto_join_rooms_when_user_is_not_the_first_real_user(
        self,
    ) -> None:
        self.store.count_real_users = AsyncMock(return_value=2)  # type: ignore[method-assign]
        self.store.is_real_user = AsyncMock(return_value=True)
        user_id = self.get_success(self.handler.register_user(localpart="real"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)

    @override_config(
        {
            "auto_join_rooms": ["#room:test"],
            "autocreate_auto_join_rooms_federated": False,
        }
    )
    def test_auto_create_auto_join_rooms_federated(self) -> None:
        """
        Auto-created rooms that are private require an invite to go to the user
        (instead of directly joining it).
        """
        room_alias_str = "#room:test"
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))

        # Ensure the room was created.
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        # Ensure the room is properly not federated.
        room = self.get_success(self.store.get_room_with_stats(room_id["room_id"]))
        assert room is not None
        self.assertFalse(room.federatable)
        self.assertFalse(room.public)
        self.assertEqual(room.join_rules, "public")
        self.assertIsNone(room.guest_access)

        # The user should be in the room.
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

    @override_config(
        {"auto_join_rooms": ["#room:test"], "auto_join_mxid_localpart": "support"}
    )
    def test_auto_join_mxid_localpart(self) -> None:
        """
        Ensure the user still needs up in the room created by a different user.
        """
        # Ensure the support user exists.
        inviter = "@support:test"

        room_alias_str = "#room:test"
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))

        # Ensure the room was created.
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        # Ensure the room is properly a public room.
        room = self.get_success(self.store.get_room_with_stats(room_id["room_id"]))
        assert room is not None
        self.assertEqual(room.join_rules, "public")

        # Both users should be in the room.
        rooms = self.get_success(self.store.get_rooms_for_user(inviter))
        self.assertIn(room_id["room_id"], rooms)
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

        # Register a second user, which should also end up in the room.
        user_id = self.get_success(self.handler.register_user(localpart="bob"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

    @override_config(
        {
            "auto_join_rooms": ["#room:test"],
            "autocreate_auto_join_room_preset": "private_chat",
            "auto_join_mxid_localpart": "support",
        }
    )
    def test_auto_create_auto_join_room_preset(self) -> None:
        """
        Auto-created rooms that are private require an invite to go to the user
        (instead of directly joining it).
        """
        # Ensure the support user exists.
        inviter = "@support:test"

        room_alias_str = "#room:test"
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))

        # Ensure the room was created.
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        # Ensure the room is properly a private room.
        room = self.get_success(self.store.get_room_with_stats(room_id["room_id"]))
        assert room is not None
        self.assertFalse(room.public)
        self.assertEqual(room.join_rules, "invite")
        self.assertEqual(room.guest_access, "can_join")

        # Both users should be in the room.
        rooms = self.get_success(self.store.get_rooms_for_user(inviter))
        self.assertIn(room_id["room_id"], rooms)
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

        # Register a second user, which should also end up in the room.
        user_id = self.get_success(self.handler.register_user(localpart="bob"))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

    @override_config(
        {
            "auto_join_rooms": ["#room:test"],
            "autocreate_auto_join_room_preset": "private_chat",
            "auto_join_mxid_localpart": "support",
        }
    )
    def test_auto_create_auto_join_room_preset_guest(self) -> None:
        """
        Auto-created rooms that are private require an invite to go to the user
        (instead of directly joining it).

        This should also work for guests.
        """
        inviter = "@support:test"

        room_alias_str = "#room:test"
        user_id = self.get_success(
            self.handler.register_user(localpart="jeff", make_guest=True)
        )

        # Ensure the room was created.
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        # Ensure the room is properly a private room.
        room = self.get_success(self.store.get_room_with_stats(room_id["room_id"]))
        assert room is not None
        self.assertFalse(room.public)
        self.assertEqual(room.join_rules, "invite")
        self.assertEqual(room.guest_access, "can_join")

        # Both users should be in the room.
        rooms = self.get_success(self.store.get_rooms_for_user(inviter))
        self.assertIn(room_id["room_id"], rooms)
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

    @override_config(
        {
            "auto_join_rooms": ["#room:test"],
            "autocreate_auto_join_room_preset": "private_chat",
            "auto_join_mxid_localpart": "support",
        }
    )
    def test_auto_create_auto_join_room_preset_invalid_permissions(self) -> None:
        """
        Auto-created rooms that are private require an invite, check that
        registration doesn't completely break if the inviter doesn't have proper
        permissions.
        """
        inviter = "@support:test"

        # Register an initial user to create the room and such (essentially this
        # is a subset of test_auto_create_auto_join_room_preset).
        room_alias_str = "#room:test"
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))

        # Ensure the room was created.
        directory_handler = self.hs.get_directory_handler()
        room_alias = RoomAlias.from_string(room_alias_str)
        room_id = self.get_success(directory_handler.get_association(room_alias))

        # Ensure the room exists.
        self.get_success(self.store.get_room_with_stats(room_id["room_id"]))

        # Both users should be in the room.
        rooms = self.get_success(self.store.get_rooms_for_user(inviter))
        self.assertIn(room_id["room_id"], rooms)
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertIn(room_id["room_id"], rooms)

        # Lower the permissions of the inviter.
        event_creation_handler = self.hs.get_event_creation_handler()
        requester = create_requester(inviter)
        event, unpersisted_context = self.get_success(
            event_creation_handler.create_event(
                requester,
                {
                    "type": "m.room.power_levels",
                    "state_key": "",
                    "room_id": room_id["room_id"],
                    "content": {"invite": 100, "users": {inviter: 0}},
                    "sender": inviter,
                },
            )
        )
        context = self.get_success(unpersisted_context.persist(event))
        self.get_success(
            event_creation_handler.handle_new_client_event(
                requester, events_and_context=[(event, context)]
            )
        )

        # Register a second user, which won't be be in the room (or even have an invite)
        # since the inviter no longer has the proper permissions.
        user_id = self.get_success(self.handler.register_user(localpart="bob"))

        # This user should not be in any rooms.
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        invited_rooms = self.get_success(
            self.store.get_invited_rooms_for_local_user(user_id)
        )
        self.assertEqual(rooms, set())
        self.assertEqual(invited_rooms, [])

    @override_config(
        {
            "user_consent": {
                "block_events_error": "Error",
                "require_at_registration": True,
            },
            "form_secret": "53cr3t",
            "public_baseurl": "http://test",
            "auto_join_rooms": ["#room:test"],
        },
    )
    def test_auto_create_auto_join_where_no_consent(self) -> None:
        """Test to ensure that the first user is not auto-joined to a room if
        they have not given general consent.
        """

        # Given:-
        #    * a user must give consent,
        #    * they have not given that consent
        #    * The server is configured to auto-join to a room
        # (and autocreate if necessary)

        # When:-
        #   * the user is registered
        user_id = self.get_success(self.handler.register_user(localpart="jeff"))

        # Then:-
        #   * Ensure that they have not been joined to the room
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 0)

        # The user provides consent; ensure they are now in the rooms.
        self.get_success(self.handler.post_consent_actions(user_id))
        rooms = self.get_success(self.store.get_rooms_for_user(user_id))
        self.assertEqual(len(rooms), 1)

    def test_register_support_user(self) -> None:
        user_id = self.get_success(
            self.handler.register_user(localpart="user", user_type=UserTypes.SUPPORT)
        )
        d = self.store.is_support_user(user_id)
        self.assertTrue(self.get_success(d))

    def test_register_not_support_user(self) -> None:
        user_id = self.get_success(self.handler.register_user(localpart="user"))
        d = self.store.is_support_user(user_id)
        self.assertFalse(self.get_success(d))

    def test_underscore_localpart_rejected_by_default(self) -> None:
        for invalid_user_id in ("_", "_prefixed"):
            with self.subTest(invalid_user_id=invalid_user_id):
                self.get_failure(
                    self.handler.register_user(localpart=invalid_user_id),
                    SynapseError,
                )

    @override_config(
        {
            "allow_underscore_prefixed_localpart": True,
        }
    )
    def test_underscore_localpart_allowed_if_configured(self) -> None:
        for valid_user_id in ("_", "_prefixed"):
            with self.subTest(valid_user_id=valid_user_id):
                user_id = self.get_success(
                    self.handler.register_user(
                        localpart=valid_user_id,
                    ),
                )
                self.assertEqual(user_id, f"@{valid_user_id}:test")

    def test_invalid_user_id(self) -> None:
        invalid_user_id = "^abcd"
        self.get_failure(
            self.handler.register_user(localpart=invalid_user_id), SynapseError
        )

    def test_special_chars(self) -> None:
        """Ensure that characters which are allowed in Matrix IDs work."""
        valid_user_id = "a1234_-./=+"
        user_id = self.get_success(self.handler.register_user(localpart=valid_user_id))
        self.assertEqual(user_id, f"@{valid_user_id}:test")

    def test_invalid_user_id_length(self) -> None:
        invalid_user_id = "x" * 256
        self.get_failure(
            self.handler.register_user(localpart=invalid_user_id), SynapseError
        )

    @override_config(
        {
            "modules": [
                {
                    "module": TestSpamChecker.__module__ + ".DenyAll",
                }
            ]
        }
    )
    def test_spam_checker_deny(self) -> None:
        """A spam checker can deny registration, which results in an error."""
        self.get_failure(self.handler.register_user(localpart="user"), SynapseError)

    @override_config(
        {
            "spam_checker": [
                {
                    "module": TestSpamChecker.__module__ + ".LegacyAllowAll",
                }
            ]
        }
    )
    def test_spam_checker_legacy_allow(self) -> None:
        """Tests that a legacy spam checker implementing the legacy 3-arg version of the
        check_registration_for_spam callback is correctly called.

        In this test and the following one we test both success and failure to make sure
        any failure comes from the spam checker (and not something else failing in the
        call stack) and any success comes from the spam checker (and not because a
        misconfiguration prevented it from being loaded).
        """
        self.get_success(self.handler.register_user(localpart="user"))

    @override_config(
        {
            "spam_checker": [
                {
                    "module": TestSpamChecker.__module__ + ".LegacyDenyAll",
                }
            ]
        }
    )
    def test_spam_checker_legacy_deny(self) -> None:
        """Tests that a legacy spam checker implementing the legacy 3-arg version of the
        check_registration_for_spam callback is correctly called.

        In this test and the previous one we test both success and failure to make sure
        any failure comes from the spam checker (and not something else failing in the
        call stack) and any success comes from the spam checker (and not because a
        misconfiguration prevented it from being loaded).
        """
        self.get_failure(self.handler.register_user(localpart="user"), SynapseError)

    @override_config(
        {
            "modules": [
                {
                    "module": TestSpamChecker.__module__ + ".BanAll",
                }
            ]
        }
    )
    def test_spam_checker_shadow_ban(self) -> None:
        """A spam checker can choose to shadow-ban a user, which allows registration to succeed."""
        user_id = self.get_success(self.handler.register_user(localpart="user"))

        # Get an access token.
        token = "testtok"
        self.get_success(
            self.store.add_access_token_to_user(
                user_id=user_id, token=token, device_id=None, valid_until_ms=None
            )
        )

        # Ensure the user was marked as shadow-banned.
        request = Mock(args={})
        request.args[b"access_token"] = [token.encode("ascii")]
        request.requestHeaders.getRawHeaders = mock_getRawHeaders()
        auth = InternalAuth(self.hs)
        requester = self.get_success(auth.get_user_by_req(request))

        self.assertTrue(requester.shadow_banned)

    @override_config(
        {
            "modules": [
                {
                    "module": TestSpamChecker.__module__ + ".BanBadIdPUser",
                }
            ]
        }
    )
    def test_spam_checker_receives_sso_type(self) -> None:
        """Test rejecting registration based on SSO type"""
        f = self.get_failure(
            self.handler.register_user(localpart="bobflimflob", auth_provider_id="cas"),
            SynapseError,
        )
        exception = f.value

        # We return 429 from the spam checker for denied registrations
        self.assertIsInstance(exception, SynapseError)
        self.assertEqual(exception.code, 429)

        # Check the same username can register using SAML
        self.get_success(
            self.handler.register_user(localpart="bobflimflob", auth_provider_id="saml")
        )

    async def get_or_create_user(
        self,
        requester: Requester,
        localpart: str,
        displayname: Optional[str],
        password_hash: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Creates a new user if the user does not exist,
        else revokes all previous access tokens and generates a new one.

        XXX: this used to be in the main codebase, but was only used by this file,
        so got moved here. TODO: get rid of it, probably

        Args:
            localpart : The local part of the user ID to register. If None,
              one will be randomly generated.
        Returns:
            A tuple of (user_id, access_token).
        """
        if localpart is None:
            raise SynapseError(400, "Request must include user id")
        await self.hs.get_auth_blocking().check_auth_blocking()
        need_register = True

        try:
            await self.handler.check_username(localpart)
        except SynapseError as e:
            if e.errcode == Codes.USER_IN_USE:
                need_register = False
            else:
                raise

        user = UserID(localpart, self.hs.hostname)
        user_id = user.to_string()
        token = self.hs.get_auth_handler().generate_access_token(user)

        if need_register:
            await self.handler.register_with_store(
                user_id=user_id,
                password_hash=password_hash,
                create_profile_with_displayname=user.localpart,
            )
        else:
            await self.hs.get_auth_handler().delete_access_tokens_for_user(user_id)

        await self.store.add_access_token_to_user(
            user_id=user_id, token=token, device_id=None, valid_until_ms=None
        )

        if displayname is not None:
            # logger.info("setting user display name: %s -> %s", user_id, displayname)
            await self.hs.get_profile_handler().set_displayname(
                user, requester, displayname, by_admin=True
            )

        return user_id, token


class RemoteAutoJoinTestCase(unittest.HomeserverTestCase):
    """Tests auto-join on remote rooms."""

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.room_id = "!roomid:remotetest"

        async def update_membership(*args: Any, **kwargs: Any) -> None:
            pass

        async def lookup_room_alias(
            *args: Any, **kwargs: Any
        ) -> Tuple[RoomID, List[str]]:
            return RoomID.from_string(self.room_id), ["remotetest"]

        self.room_member_handler = Mock(spec=["update_membership", "lookup_room_alias"])
        self.room_member_handler.update_membership.side_effect = update_membership
        self.room_member_handler.lookup_room_alias.side_effect = lookup_room_alias

        hs = self.setup_test_homeserver(room_member_handler=self.room_member_handler)
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.handler = self.hs.get_registration_handler()
        self.store = self.hs.get_datastores().main

    @override_config({"auto_join_rooms": ["#room:remotetest"]})
    def test_auto_create_auto_join_remote_room(self) -> None:
        """Tests that we don't attempt to create remote rooms, and that we don't attempt
        to invite ourselves to rooms we're not in."""

        # Register a first user; this should call _create_and_join_rooms
        self.get_success(self.handler.register_user(localpart="jeff"))

        _, kwargs = self.room_member_handler.update_membership.call_args

        self.assertEqual(kwargs["room_id"], self.room_id)
        self.assertEqual(kwargs["action"], "join")
        self.assertEqual(kwargs["remote_room_hosts"], ["remotetest"])

        # Register a second user; this should call _join_rooms
        self.get_success(self.handler.register_user(localpart="jeff2"))

        _, kwargs = self.room_member_handler.update_membership.call_args

        self.assertEqual(kwargs["room_id"], self.room_id)
        self.assertEqual(kwargs["action"], "join")
        self.assertEqual(kwargs["remote_room_hosts"], ["remotetest"])
