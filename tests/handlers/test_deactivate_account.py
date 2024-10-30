#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.constants import AccountDataTypes, EventTypes, JoinRules, Membership
from synapse.push.rulekinds import PRIORITY_CLASS_MAP
from synapse.rest import admin
from synapse.rest.client import account, login, room
from synapse.server import HomeServer
from synapse.synapse_rust.push import PushRule
from synapse.types import UserID, create_requester
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class DeactivateAccountTestCase(HomeserverTestCase):
    servlets = [
        login.register_servlets,
        admin.register_servlets,
        account.register_servlets,
        room.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self._store = hs.get_datastores().main

        self.user = self.register_user("user", "pass")
        self.token = self.login("user", "pass")
        self.handler = self.hs.get_room_member_handler()

    def _deactivate_my_account(self) -> None:
        """
        Deactivates the account `self.user` using `self.token` and asserts
        that it returns a 200 success code.
        """
        req = self.make_request(
            "POST",
            "account/deactivate",
            {
                "auth": {
                    "type": "m.login.password",
                    "user": self.user,
                    "password": "pass",
                },
                "erase": True,
            },
            access_token=self.token,
        )

        self.assertEqual(req.code, 200, req)

    def test_global_account_data_deleted_upon_deactivation(self) -> None:
        """
        Tests that global account data is removed upon deactivation.
        """
        # Add some account data
        self.get_success(
            self._store.add_account_data_for_user(
                self.user,
                AccountDataTypes.DIRECT,
                {"@someone:remote": ["!somewhere:remote"]},
            )
        )

        # Check that we actually added some.
        self.assertIsNotNone(
            self.get_success(
                self._store.get_global_account_data_by_type_for_user(
                    self.user, AccountDataTypes.DIRECT
                )
            ),
        )

        # Request the deactivation of our account
        self._deactivate_my_account()

        # Check that the account data does not persist.
        self.assertIsNone(
            self.get_success(
                self._store.get_global_account_data_by_type_for_user(
                    self.user, AccountDataTypes.DIRECT
                )
            ),
        )

    def test_room_account_data_deleted_upon_deactivation(self) -> None:
        """
        Tests that room account data is removed upon deactivation.
        """
        room_id = "!room:test"

        # Add some room account data
        self.get_success(
            self._store.add_account_data_to_room(
                self.user,
                room_id,
                "m.fully_read",
                {"event_id": "$aaaa:test"},
            )
        )

        # Check that we actually added some.
        self.assertIsNotNone(
            self.get_success(
                self._store.get_account_data_for_room_and_type(
                    self.user, room_id, "m.fully_read"
                )
            ),
        )

        # Request the deactivation of our account
        self._deactivate_my_account()

        # Check that the account data does not persist.
        self.assertIsNone(
            self.get_success(
                self._store.get_account_data_for_room_and_type(
                    self.user, room_id, "m.fully_read"
                )
            ),
        )

    def _is_custom_rule(self, push_rule: PushRule) -> bool:
        """
        Default rules start with a dot: such as .m.rule and .im.vector.
        This function returns true iff a rule is custom (not default).
        """
        return "/." not in push_rule.rule_id

    def test_push_rules_deleted_upon_account_deactivation(self) -> None:
        """
        Push rules are a special case of account data.
        They are stored separately but get sent to the client as account data in /sync.
        This tests that deactivating a user deletes push rules along with the rest
        of their account data.
        """

        # Add a push rule
        self.get_success(
            self._store.add_push_rule(
                self.user,
                "personal.override.rule1",
                PRIORITY_CLASS_MAP["override"],
                [],
                [],
            )
        )

        # Test the rule exists
        filtered_push_rules = self.get_success(
            self._store.get_push_rules_for_user(self.user)
        )
        # Filter out default rules; we don't care
        push_rules = [
            r for r, _ in filtered_push_rules.rules() if self._is_custom_rule(r)
        ]
        # Check our rule made it
        self.assertEqual(len(push_rules), 1)
        self.assertEqual(push_rules[0].rule_id, "personal.override.rule1")
        self.assertEqual(push_rules[0].priority_class, 5)
        self.assertEqual(push_rules[0].conditions, [])
        self.assertEqual(push_rules[0].actions, [])

        # Request the deactivation of our account
        self._deactivate_my_account()

        filtered_push_rules = self.get_success(
            self._store.get_push_rules_for_user(self.user)
        )
        # Filter out default rules; we don't care
        push_rules = [
            r for r, _ in filtered_push_rules.rules() if self._is_custom_rule(r)
        ]
        # Check our rule no longer exists
        self.assertEqual(push_rules, [], push_rules)

    def test_ignored_users_deleted_upon_deactivation(self) -> None:
        """
        Ignored users are a special case of account data.
        They get denormalised into the `ignored_users` table upon being stored as
        account data.
        Test that a user's list of ignored users is deleted upon deactivation.
        """

        # Add an ignored user
        self.get_success(
            self._store.add_account_data_for_user(
                self.user,
                AccountDataTypes.IGNORED_USER_LIST,
                {"ignored_users": {"@sheltie:test": {}}},
            )
        )

        # Test the user is ignored
        self.assertEqual(
            self.get_success(self._store.ignored_by("@sheltie:test")), {self.user}
        )

        # Request the deactivation of our account
        self._deactivate_my_account()

        # Test the user is no longer ignored by the user that was deactivated
        self.assertEqual(
            self.get_success(self._store.ignored_by("@sheltie:test")), set()
        )

    def _rerun_retroactive_account_data_deletion_update(self) -> None:
        # Reset the 'all done' flag
        self._store.db_pool.updates._all_done = False

        self.get_success(
            self._store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": "delete_account_data_for_deactivated_users",
                    "progress_json": "{}",
                },
            )
        )

        self.wait_for_background_updates()

    def test_account_data_deleted_retroactively_by_background_update_if_deactivated(
        self,
    ) -> None:
        """
        Tests that a user, who deactivated their account before account data was
        deleted automatically upon deactivation, has their account data retroactively
        scrubbed by the background update.
        """

        # Request the deactivation of our account
        self._deactivate_my_account()

        # Add some account data
        # (we do this after the deactivation so that the act of deactivating doesn't
        # clear it out. This emulates a user that was deactivated before this was cleared
        # upon deactivation.)
        self.get_success(
            self._store.add_account_data_for_user(
                self.user,
                AccountDataTypes.DIRECT,
                {"@someone:remote": ["!somewhere:remote"]},
            )
        )

        # Check that the account data is there.
        self.assertIsNotNone(
            self.get_success(
                self._store.get_global_account_data_by_type_for_user(
                    self.user,
                    AccountDataTypes.DIRECT,
                )
            ),
        )

        # Re-run the retroactive deletion update
        self._rerun_retroactive_account_data_deletion_update()

        # Check that the account data was cleared.
        self.assertIsNone(
            self.get_success(
                self._store.get_global_account_data_by_type_for_user(
                    self.user,
                    AccountDataTypes.DIRECT,
                )
            ),
        )

    def test_account_data_preserved_by_background_update_if_not_deactivated(
        self,
    ) -> None:
        """
        Tests that the background update does not scrub account data for users that have
        not been deactivated.
        """

        # Add some account data
        # (we do this after the deactivation so that the act of deactivating doesn't
        # clear it out. This emulates a user that was deactivated before this was cleared
        # upon deactivation.)
        self.get_success(
            self._store.add_account_data_for_user(
                self.user,
                AccountDataTypes.DIRECT,
                {"@someone:remote": ["!somewhere:remote"]},
            )
        )

        # Check that the account data is there.
        self.assertIsNotNone(
            self.get_success(
                self._store.get_global_account_data_by_type_for_user(
                    self.user,
                    AccountDataTypes.DIRECT,
                )
            ),
        )

        # Re-run the retroactive deletion update
        self._rerun_retroactive_account_data_deletion_update()

        # Check that the account data was NOT cleared.
        self.assertIsNotNone(
            self.get_success(
                self._store.get_global_account_data_by_type_for_user(
                    self.user,
                    AccountDataTypes.DIRECT,
                )
            ),
        )

    def test_deactivate_account_needs_auth(self) -> None:
        """
        Tests that making a request to /deactivate with an empty body
        succeeds in starting the user-interactive auth flow.
        """
        req = self.make_request(
            "POST",
            "account/deactivate",
            {},
            access_token=self.token,
        )

        self.assertEqual(req.code, 401, req)
        self.assertEqual(req.json_body["flows"], [{"stages": ["m.login.password"]}])

    def test_deactivate_account_rejects_invites(self) -> None:
        """
        Tests that deactivating an account rejects its invite memberships
        """
        # Create another user and room just for the invitation
        another_user = self.register_user("another_user", "pass")
        token = self.login("another_user", "pass")
        room_id = self.helper.create_room_as(another_user, is_public=False, tok=token)

        # Invite user to the created room
        invite_event, _ = self.get_success(
            self.handler.update_membership(
                requester=create_requester(another_user),
                target=UserID.from_string(self.user),
                room_id=room_id,
                action=Membership.INVITE,
            )
        )

        # Check that the invite exists
        invite = self.get_success(
            self._store.get_invited_rooms_for_local_user(self.user)
        )
        self.assertEqual(invite[0].event_id, invite_event)

        # Deactivate the user
        self._deactivate_my_account()

        # Check that the deactivated user has no invites in the room
        after_deactivate_invite = self.get_success(
            self._store.get_invited_rooms_for_local_user(self.user)
        )
        self.assertEqual(len(after_deactivate_invite), 0)

    def test_deactivate_account_rejects_knocks(self) -> None:
        """
        Tests that deactivating an account rejects its knock memberships
        """
        # Create another user and room just for the invitation
        another_user = self.register_user("another_user", "pass")
        token = self.login("another_user", "pass")
        room_id = self.helper.create_room_as(
            another_user,
            is_public=False,
            tok=token,
        )

        # Allow room to be knocked at
        self.helper.send_state(
            room_id,
            EventTypes.JoinRules,
            {"join_rule": JoinRules.KNOCK},
            tok=token,
        )

        # Knock user at the created room
        knock_event, _ = self.get_success(
            self.handler.update_membership(
                requester=create_requester(self.user),
                target=UserID.from_string(self.user),
                room_id=room_id,
                action=Membership.KNOCK,
            )
        )

        # Check that the knock exists
        knocks = self.get_success(
            self._store.get_knocked_at_rooms_for_local_user(self.user)
        )
        self.assertEqual(knocks[0].event_id, knock_event)

        # Deactivate the user
        self._deactivate_my_account()

        # Check that the deactivated user has no knocks
        after_deactivate_knocks = self.get_success(
            self._store.get_knocked_at_rooms_for_local_user(self.user)
        )
        self.assertEqual(len(after_deactivate_knocks), 0)

    def test_membership_is_redacted_upon_deactivation(self) -> None:
        """
        Tests that room membership events are redacted if erasure is requested.
        """
        # Create a room
        room_id = self.helper.create_room_as(
            self.user,
            is_public=True,
            tok=self.token,
        )

        # Change the displayname
        membership_event, _ = self.get_success(
            self.handler.update_membership(
                requester=create_requester(self.user),
                target=UserID.from_string(self.user),
                room_id=room_id,
                action=Membership.JOIN,
                content={"displayname": "Hello World!"},
            )
        )

        # Deactivate the account
        self._deactivate_my_account()

        # Get the all membership event IDs
        membership_event_ids = self.get_success(
            self._store.get_membership_event_ids_for_user(self.user, room_id=room_id)
        )

        # Get the events incl. JSON
        events = self.get_success(self._store.get_events_as_list(membership_event_ids))

        # Validate that there is no displayname in any of the events
        for event in events:
            self.assertTrue("displayname" not in event.content)

    def test_rooms_forgotten_upon_deactivation(self) -> None:
        """
        Tests that the user 'forgets' the rooms they left upon deactivation.
        """
        # Create a room
        room_id = self.helper.create_room_as(
            self.user,
            is_public=True,
            tok=self.token,
        )

        # Deactivate the account
        self._deactivate_my_account()

        # Get all of the user's forgotten rooms
        forgotten_rooms = self.get_success(
            self._store.get_forgotten_rooms_for_user(self.user)
        )

        # Validate that the created room is forgotten
        self.assertTrue(room_id in forgotten_rooms)
