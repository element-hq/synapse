#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 New Vector, Ltd
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

from parameterized import parameterized, parameterized_class

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import ProfileFields
from synapse.rest.client import knock, login, profile, room, sync
from synapse.server import HomeServer
from synapse.types import UserID, create_requester
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase
from tests.unittest import override_config

logger = logging.getLogger(__name__)


# FIXME: This can be removed once we bump `SCHEMA_COMPAT_VERSION` and run the
# foreground update for
# `sliding_sync_joined_rooms`/`sliding_sync_membership_snapshots` (tracked by
# https://github.com/element-hq/synapse/issues/17623)
@parameterized_class(
    ("use_new_tables",),
    [
        (True,),
        (False,),
    ],
    class_name_func=lambda cls,
    num,
    params_dict: f"{cls.__name__}_{'new' if params_dict['use_new_tables'] else 'fallback'}",
)
class SlidingSyncProfilesTestCase(SlidingSyncBase):
    """Tests for the profile updates sliding sync extension"""

    servlets = [
        synapse.rest.admin.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        profile.register_servlets,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.profile_handler = self.hs.get_profile_handler()
        self.user = self.register_user("user", "password")
        self.tok = self.login("user", "password")
        self.other_user = self.register_user("other_user", "password")
        self.other_tok = self.login("other_user", "password")
        self.joined_room = self.helper.create_room_as(self.user, tok=self.tok)
        self.helper.join(
            room=self.joined_room, user=self.other_user, tok=self.other_tok
        )
        super().prepare(reactor, clock, hs)

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    def test_no_data_when_not_enabled(self, is_initial: bool) -> None:
        """
        Test that no profile extension response is returned
        if the feature is not enabled.
        """
        if is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        self.assertIsNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles")
        )

        if not is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)

            self.assertIsNone(
                response_body["extensions"].get("org.matrix.msc4262.profiles")
            )

    @override_config({"include_profile_updates_in_sync": True})
    def test_no_data_initial_sync(self) -> None:
        """
        Test that enabling the profiles extension works during an initial sync,
        even if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                },
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)
        self.assertIsNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles")
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_no_data_incremental_sync(self) -> None:
        """
        Test that enabling profiles extension works during an incremental sync, even
        if there is no-data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")

        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                }
            },
        }
        _, from_token = self.do_sync(sync_body, tok=user1_tok)

        # Make an incremental Sliding Sync request with the profiles extension enabled
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=user1_tok)

        self.assertIsNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles")
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_updated_fields_are_sent(self, is_initial: bool) -> None:
        """
        Test that profile extension response returns field updates
        in incremental and initial sync.
        """
        if is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                self.joined_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            },
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        if is_initial:
            self.assertEqual(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                    "@other_user:test"
                ],
                {
                    "updated": {
                        "field": "value",
                    }
                },
            )
        else:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
            # We don't include room subscriptions, as we want to see updates coming
            # through even without room subscriptions
            del sync_body["room_subscriptions"]
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)

            self.assertEqual(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                    "@other_user:test"
                ],
                {
                    "updated": {
                        "field": "value",
                    }
                },
            )

    @override_config({"include_profile_updates_in_sync": True})
    def test_updated_field_then_deleted_does_not_error(self) -> None:
        """
        Test that profile extension response does not crash if the user first
        updates a field, then deletes it, and then the sync happens seeing both
        the update and delete in the stream.
        """
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="field",
                new_value="value",
            )
        )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)

        # Update field
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="field",
                new_value="new value",
            )
        )
        # Delete field
        self.get_success(
            self.profile_handler.delete_profile_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="field",
            )
        )

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
        self.assertEqual(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@other_user:test"
            ],
            {
                "removed": [
                    "field",
                ],
            },
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_updated_fields_are_not_sent_if_not_requested(
        self, is_initial: bool
    ) -> None:
        """
        Test that profile extension response doesn't return field updates we didn't
        request in initial and incremental sync.
        """
        if is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="anotherfield",
                    new_value="value",
                )
            )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        if is_initial:
            # Nothing returned since we didn't ask for the updated field
            self.assertIsNone(
                response_body["extensions"].get("org.matrix.msc4262.profiles")
            )

        if not is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="anotherfield",
                    new_value="value",
                )
            )
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
            # Nothing returned since we didn't ask for the updated field
            self.assertIsNone(
                response_body["extensions"].get("org.matrix.msc4262.profiles")
            )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_updated_fields_are_not_included_if_not_in_requested_rooms(
        self, is_initial: bool
    ) -> None:
        """
        Test that profile extension response respects the room subscriptions, by:
        * for initial sync returning updates for only those users in the given rooms
        * for incremental sync returning all updates in shared rooms
        """
        new_room = self.helper.create_room_as(self.user, tok=self.tok)
        if is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                new_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            },
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                    "fields": ["field"],
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        if is_initial:
            # Nothing returned since even though user and other_user share a room,
            # we didn't ask for that room.
            self.assertIsNone(
                response_body["extensions"].get("org.matrix.msc4262.profiles")
            )

        if not is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
            # Even though we only asked for a room other_user is not in,
            # since these users share a room, updates are always sent via incremental
            # sync.
            self.assertEqual(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                    "@other_user:test"
                ],
                {
                    "updated": {
                        "field": "value",
                    }
                },
            )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_all_fields_returned_if_no_fields_specified(self, is_initial: bool) -> None:
        """
        Test that profile extension response returns all profile fields if we didn't
        request any particular fields in initial and incremental sync.
        """
        if is_initial:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            # We need to ensure a room is included to get things back in initial sync
            "room_subscriptions": {
                self.joined_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            },
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        if is_initial:
            # As this is an initial sync, we get all profile fields
            self.assertEqual(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                    "@other_user:test"
                ],
                {
                    "updated": {
                        "avatar_url": None,
                        "displayname": "other_user",
                        "field": "value",
                    }
                },
            )

        else:
            self.get_success(
                self.profile_handler.set_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name="field",
                    new_value="value",
                )
            )
            # We don't include room subscriptions, as we want to see updates coming
            # through even without room subscriptions
            del sync_body["room_subscriptions"]
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
            # As this is an incremental sync, we only get actual updates back
            self.assertEqual(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                    "@other_user:test"
                ],
                {
                    "updated": {
                        "field": "value",
                    }
                },
            )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_null_profile_returned_if_user_left_all_rooms(
        self,
        request_fields: bool,
    ) -> None:
        """
        Test that profile extension response returns a null for the user in
        incremental sync.
        """
        # Make an initial Sliding Sync request with the profiles extension enabled
        profiles_config: dict = {
            "enabled": True,
        }
        if request_fields:
            profiles_config["fields"] = ["field"]
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": profiles_config,
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)

        self.helper.leave(self.joined_room, self.other_user, tok=self.other_tok)

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
        # We should see a null profile
        self.assertIsNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@other_user:test"
            ],
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_profile_returned_if_user_left_then_rejoined(self) -> None:
        """
        Test that the profile extension response returns a profile, rather than a
        null, for a user that left their last shared room and then rejoined it
        within the same incremental sync window.
        """
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {"enabled": True},
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)

        self.helper.leave(self.joined_room, self.other_user, tok=self.other_tok)
        self.helper.join(self.joined_room, self.other_user, tok=self.other_tok)

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
        # The rejoin overrides the leave, so we should see the full profile rather
        # than a null profile.
        self.assertEqual(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@other_user:test"
            ],
            {
                "updated": {
                    "displayname": "other_user",
                    # FIXME: This shouldn't be returned, but currently is
                    "avatar_url": None,
                }
            },
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_all_fields_returned_in_incremental_non_lazy_sync_if_someone_joined(
        self, request_fields: bool
    ) -> None:
        """
        Test that profile extension response returns all profile fields in
        incremental non-lazy sync, if someone joined the room..
        """
        # Make an initial Sliding Sync request with the profiles extension enabled
        profiles_config: dict = {
            "enabled": True,
        }
        if request_fields:
            profiles_config["fields"] = ["displayname"]
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": profiles_config,
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)

        third_user = self.register_user("third_user", "third_user")
        third_tok = self.login(third_user, "third_user")
        self.helper.join(self.joined_room, third_user, tok=third_tok)

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)

        expectation = {
            "updated": {
                "avatar_url": None,
                "displayname": "third_user",
            }
        }
        if request_fields:
            expectation = {
                "updated": {
                    "displayname": "third_user",
                },
            }
        self.assertEqual(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@third_user:test"
            ],
            expectation,
        )

    @parameterized.expand(["displayname", "avatar_url", "someotherfield"])
    @override_config({"include_profile_updates_in_sync": True})
    def test_removed_fields_get_sent_down_as_removed(
        self,
        field_name: str,
    ) -> None:
        """
        Test that we deliver clear/removed fields in the "removed" key in the response.
        """
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name=field_name,
                new_value="value",
            )
        )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)

        # Delete the field
        if field_name in (ProfileFields.DISPLAYNAME, ProfileFields.AVATAR_URL):
            self.get_success(
                self.profile_handler.set_profile_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name=field_name,
                    new_value=None,
                )
            )
        else:
            self.get_success(
                self.profile_handler.delete_profile_field(
                    target_user=UserID.from_string(self.other_user),
                    requester=create_requester(self.other_user),
                    field_name=field_name,
                )
            )

        # Make an incremental Sliding Sync request
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
        # We should see the removed field
        self.assertEqual(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@other_user:test"
            ],
            {
                "removed": [
                    field_name,
                ],
            },
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_updated_key_only_present_if_updates(self) -> None:
        """
        > The updated field SHOULD only be present if there are changes to existing fields on a user's profile.
        """
        self.skipTest("Not yet implemented")

    @override_config({"include_profile_updates_in_sync": True})
    def test_rooms_subset_changing_includes_full_profile(self) -> None:
        """
        > When a room enters this subset in this connection for the first time, all requested
        > fields from profiles of users in that room MAY be sent down. This gives the client
        > a base set of information for which future field updates can be applied on top of.
        > The homeserver MAY omit some fields and profiles if it believes that the client has
        > already received them, likewise repeat profiles MAY be sent down based on homeserver
        > implementation.
        """
        self.skipTest("Not yet implemented")

    @override_config({"include_profile_updates_in_sync": True})
    def test_fields_subset_changing_sends_down_field_even_if_not_changed(self) -> None:
        """
        > Finally, if the list of fields expands to cover a new field ID, those fields should
        > be sent down for all users that are within the current room subset. Future incremental
        > updates will then include changes to this field.
        """
        self.skipTest("Not yet implemented")

    @parameterized.expand(
        [
            [True, True],
            [True, False],
            [False, False],
            [False, True],
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_down_full_profile_if_events_in_timeline(
        self,
        is_initial: bool,
        use_room_subsciptions: bool,
    ) -> None:
        """
        Test that when lazy loading, only those members who have events in
        the timeline get their profiles sent down in the sync response, for
        rooms configured with lazy loading.

        Rooms without lazy loading should include all the members in initial sync,
        none in incremental.
        """
        # Create three users to fill the heroes
        # Our heroes will thus be user, other_user and the three heroes here.
        for i in range(3):
            user = self.register_user(f"hero{i}", "password")
            tok = self.login(f"hero{i}", "password")
            self.helper.join(self.joined_room, user=user, tok=tok)
        third_user = self.register_user("third_user", "password")
        third_tok = self.login("third_user", "password")
        fourth_user = self.register_user("fourth_user", "password")
        fourth_tok = self.login("fourth_user", "password")
        fifth_user = self.register_user("fifth_user", "password")
        fifth_tok = self.login("fifth_user", "password")
        self.helper.join(
            room=self.joined_room,
            user=third_user,
            tok=third_tok,
        )
        self.helper.join(
            room=self.joined_room,
            user=fifth_user,
            tok=fifth_tok,
        )
        new_room = self.helper.create_room_as(self.user, tok=self.tok)
        self.helper.join(
            room=new_room,
            user=fourth_user,
            tok=fourth_tok,
        )
        if is_initial:
            self.helper.send_messages(
                room_id=self.joined_room, num_events=1, tok=self.other_tok
            )
            self.helper.send_messages(
                room_id=self.joined_room, num_events=10, tok=third_tok
            )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body: dict[str, dict] = {
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        if use_room_subsciptions:
            sync_body["room_subscriptions"] = {
                self.joined_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
                new_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            }
        else:
            sync_body["lists"] = {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [],
                    "timeline_limit": 10,
                }
            }
            # We also need to specifically request the non-lazy room, otherwise
            # our test to see if non-lazy members are also included will fail
            sync_body["room_subscriptions"] = {
                new_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            }
        if is_initial:
            if use_room_subsciptions:
                sync_body["room_subscriptions"][self.joined_room]["required_state"] = [
                    ["m.room.member", "$LAZY"],
                    # Don't request other state as we're checking timeline events
                    # ["*", "*"],
                ]
            else:
                sync_body["lists"]["foo-list"]["required_state"] = [
                    ["m.room.member", "$LAZY"],
                    # Don't request other state as we're checking timeline events
                    # ["*", "*"],
                ]
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        if is_initial:
            self.assertIsNotNone(
                response_body["extensions"].get("org.matrix.msc4262.profiles")
            )
            # Other user is a hero so should be included.
            self.assertIsNotNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@other_user:test"
                )
            )
            # Third user has events in the timeline, so should be here.
            self.assertIsNotNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@third_user:test"
                )
            )
            # Initial sync always includes ourselves
            self.assertIsNotNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@user:test"
                )
            )
            # Fourth user is a member of a non-lazy configured room, so should be here.
            self.assertIsNotNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@fourth_user:test"
                )
            )
            # Fifth user should be filtered out as they have no events in the room.
            self.assertIsNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@fifth_user:test"
                )
            )

        if not is_initial:
            self.helper.send_messages(
                room_id=self.joined_room, num_events=1, tok=self.other_tok
            )
            self.helper.send_messages(
                room_id=self.joined_room, num_events=10, tok=third_tok
            )
            if use_room_subsciptions:
                sync_body["room_subscriptions"][self.joined_room]["required_state"] = [
                    ["m.room.member", "$LAZY"],
                    # Don't request other state as we're checking timeline events
                    # ["*", "*"],
                ]
            else:
                sync_body["lists"]["foo-list"]["required_state"] = [
                    ["m.room.member", "$LAZY"],
                    # Don't request other state as we're checking timeline events
                    # ["*", "*"],
                ]
            # Make an incremental Sliding Sync request
            response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
            self.assertIsNotNone(
                response_body["extensions"].get("org.matrix.msc4262.profiles")
            )
            # TODO check this if it's expected that heroes come down differently
            # depending on if using a room subscription or a list
            if use_room_subsciptions:
                # Other user should be filtered out as heroes don't come down
                # in incremental sync in the same way as initial sync, if the
                # room is included via a room subscription.
                self.assertIsNone(
                    response_body["extensions"]["org.matrix.msc4262.profiles"][
                        "users"
                    ].get("@other_user:test")
                )
            else:
                # Other user should be included as heroes do come down
                # in incremental sync in the same way as initial sync when the
                # room is included in a list
                self.assertIsNotNone(
                    response_body["extensions"]["org.matrix.msc4262.profiles"][
                        "users"
                    ].get("@other_user:test")
                )
            # Third user has events in the timeline, so should be here.
            self.assertIsNotNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@third_user:test"
                )
            )
            # We are not included ourselves in incremental sync without updates.
            self.assertIsNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@user:test"
                )
            )
            # Fourth user is a member of a non-lazy configured room, but had no updates,
            # so shouldn't be here.
            self.assertIsNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@fourth_user:test"
                )
            )
            # Fifth user should be excluded as they have no events.
            self.assertIsNone(
                response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                    "@fifth_user:test"
                )
            )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_full_profile_even_if_no_events_if_otherwise_included(
        self,
        use_room_subsciptions: bool,
    ) -> None:
        """
        Test that when lazy loading, if a user is in both a lazy loading room
        and a non-lazy configured room, even if there are no events in the timeline,
        their profile is sent down.

        This test only makes sense for initial sync, as for incremental we would
        not expect to see users without timeline events if they had no profile updates.
        """
        # Create some users to fill the heroes so they don't pollute the test.
        for i in range(3):
            user = self.register_user(f"hero{i}", "password")
            tok = self.login(f"hero{i}", "password")
            self.helper.join(self.joined_room, user=user, tok=tok)
        new_user = self.register_user("new_user", password="password")
        new_tok = self.login("new_user", password="password")
        new_room = self.helper.create_room_as(self.user, tok=self.tok)
        self.helper.join(
            room=self.joined_room,
            user=new_user,
            tok=new_tok,
        )
        self.helper.join(
            room=new_room,
            user=new_user,
            tok=new_tok,
        )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body: dict[str, dict] = {
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        if use_room_subsciptions:
            sync_body["room_subscriptions"] = {
                self.joined_room: {
                    "required_state": [
                        ["m.room.member", "$LAZY"],
                        # Don't request any events for this room
                        # ["*", "*"],
                    ],
                    # Force zero timeline events in the response, otherwise
                    # this test wont work, as the timeline_events in the room
                    # response will contain all the create/join etc events too.
                    "timeline_limit": 0,
                },
                new_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            }
        else:
            sync_body["lists"] = {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        ["m.room.member", "$LAZY"],
                        # Don't request any events for this room
                        # ["*", "*"],
                    ],
                    # Force zero timeline events in the response, otherwise
                    # this test wont work, as the timeline_events in the room
                    # response will contain all the create/join etc events too.
                    "timeline_limit": 0,
                }
            }
            # We also need to specifically request the non-lazy room, otherwise
            # our test to see if non-lazy members are also included will fail
            sync_body["room_subscriptions"] = {
                new_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        self.assertIsNotNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles")
        )
        # New user should be included as they are in a non-lazy room too,
        # even though the lazy configured room had no events.
        self.assertIsNotNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@new_user:test"
            )
        )
        # Initial sync always includes ourselves
        self.assertIsNotNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@user:test"
            )
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_full_profile_for_required_state_member_events(
        self,
        use_room_subsciptions: bool,
    ) -> None:
        """
        Test that when lazy loading for lazy rooms, even without timeline events,
        we get profiles for users who have membership events in required_state.

        This test only makes sense for initial sync, as for incremental this would
        happen via the `ProfileUpdateAction.JOINED_ROOM` events.
        """
        # Create some users to fill the heroes so they don't pollute the test.
        for i in range(3):
            user = self.register_user(f"hero{i}", "password")
            tok = self.login(f"hero{i}", "password")
            self.helper.join(self.joined_room, user=user, tok=tok)
        new_user = self.register_user("new_user", password="password")
        new_tok = self.login("new_user", password="password")
        self.helper.join(
            room=self.joined_room,
            user=new_user,
            tok=new_tok,
        )
        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body: dict[str, dict] = {
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        if use_room_subsciptions:
            sync_body["room_subscriptions"] = {
                self.joined_room: {
                    "required_state": [
                        ["m.room.member", "$LAZY"],
                        ["*", "*"],
                    ],
                    # Force zero timeline events in the response, otherwise
                    # this test wont work, as the timeline_events in the room
                    # response will contain all the create/join etc events too.
                    "timeline_limit": 0,
                },
            }
        else:
            sync_body["lists"] = {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        ["m.room.member", "$LAZY"],
                        ["*", "*"],
                    ],
                    # Force zero timeline events in the response, otherwise
                    # this test wont work, as the timeline_events in the room
                    # response will contain all the create/join etc events too.
                    "timeline_limit": 0,
                }
            }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        self.assertIsNotNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles")
        )
        # New user should be included as they joined the room and as such
        # have membership events in required_state.
        self.assertIsNotNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@new_user:test"
            )
        )
        # Initial sync always includes ourselves
        self.assertIsNotNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@user:test"
            )
        )

    @parameterized.expand(
        [
            True,
            False,
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_full_profile_for_heroes(
        self,
        use_room_subsciptions: bool,
    ) -> None:
        """
        Test that when lazy loading for lazy rooms, even without timeline events or
        required_state, we get profiles for room heroes.

        This test must ensure heroes don't get included in timeline_events
        or required_state.

        This test only makes sense for initial sync, as for incremental sync
        Synapse doesn't generate a room response without requesting state or
        timeline events, thus no heroes either.
        """
        # Create some users to fill the heroes
        for i in range(4):
            user = self.register_user(f"hero{i}", "password")
            tok = self.login(f"hero{i}", "password")
            self.helper.join(self.joined_room, user=user, tok=tok)
        not_hero = self.register_user("not_hero", "password")
        not_hero_tok = self.login("not_hero", "password")
        self.helper.join(self.joined_room, user=not_hero, tok=not_hero_tok)

        # Make an initial Sliding Sync request with the profiles extension enabled
        sync_body: dict[str, dict] = {
            "extensions": {
                "org.matrix.msc4262.profiles": {
                    "enabled": True,
                },
            },
        }
        if use_room_subsciptions:
            sync_body["room_subscriptions"] = {
                self.joined_room: {
                    "required_state": [
                        ["m.room.member", "$LAZY"],
                        # Don't request any events for this room
                        # ["*", "*"],
                    ],
                    # Force zero timeline events in the response, otherwise
                    # this test wont work, as the timeline_events in the room
                    # response will contain all the create/join etc events too.
                    "timeline_limit": 0,
                },
            }
        else:
            sync_body["lists"] = {
                "foo-list": {
                    "ranges": [[0, 0]],
                    "required_state": [
                        ["m.room.member", "$LAZY"],
                        # Don't request any events for this room
                        # ["*", "*"],
                    ],
                    # Force zero timeline events in the response, otherwise
                    # this test wont work, as the timeline_events in the room
                    # response will contain all the create/join etc events too.
                    "timeline_limit": 0,
                }
            }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)
        self.assertIsNotNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles")
        )
        # Other user should be included as they are a room hero
        self.assertIsNotNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@other_user:test"
            )
        )
        # Not hero user should be excluded as they're not a hero
        self.assertIsNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@not_hero:test"
            )
        )
        # Initial sync always includes ourselves
        self.assertIsNotNone(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"].get(
                "@user:test"
            )
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_repeat_of_sync_correctly_includes_profile_information_again(self) -> None:
        """
        > Homeservers should only consider a profile field update "accepted" by a client
        > once the client returns with a new /sync request with the next /sync token,
        > NOT just after sending down the profile update. The client may never receive
        > response due to network conditions, or a bug in the client implementation.
        """
        self.skipTest("Not yet implemented")
