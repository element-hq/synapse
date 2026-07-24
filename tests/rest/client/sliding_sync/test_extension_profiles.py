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
from synapse.rest.client import login, profile, room, sync
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
        # Starting situation
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

        # FIXME: once field deletions come down in sync this should
        # be checking for that
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
    def test_updated_fields_are_not_if_not_in_requested_rooms(
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
            [True, False],
            [True, True],
            [False, False],
            [False, True],
        ]
    )
    @override_config({"include_profile_updates_in_sync": True})
    def test_null_profile_returned_if_user_left_all_rooms(
        self, request_fields: bool, is_lazy: bool
    ) -> None:
        """
        Test that profile extension response returns a null for the user in
        incremental sync.
        """
        # TODO handle is_lazy
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

    @override_config({"include_profile_updates_in_sync": True})
    def test_tracking_of_sent_fields_per_sliding_sync_connection(self) -> None:
        """
        > Homeservers are encouraged to maintain a record of which user profile fields
        > have been sent down in a given sliding sync connection. That way, as new rooms
        > enter the room subset, users who were already in rooms within the room subset
        > will not have their full profiles sent down a second time.
        """
        new_room = self.helper.create_room_as(self.user, tok=self.tok)
        # Make an initial Sliding Sync request with the profiles extension enabled
        profiles_config: dict = {
            "enabled": True,
        }
        sync_body = {
            "lists": {},
            "room_subscriptions": {
                self.joined_room: {
                    "required_state": [],
                    "timeline_limit": 10,
                },
            },
            "extensions": {
                "org.matrix.msc4262.profiles": profiles_config,
            },
        }
        response_body, from_token = self.do_sync(sync_body, tok=self.tok)

        # Starting situation, we get the full profile
        self.assertEqual(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@other_user:test"
            ],
            {
                "updated": {
                    "avatar_url": None,
                    "displayname": "other_user",
                },
            },
        )

        # Join other user to the new room and sync now asking for both
        self.helper.join(new_room, self.other_user, tok=self.other_tok)
        sync_body["room_subscriptions"][new_room] = {
            "required_state": [],
            "timeline_limit": 10,
        }

        # Make an incremental Sliding Sync request
        response_body, from_token = self.do_sync(
            sync_body, since=from_token, tok=self.tok
        )
        # We should not get other user re-sent
        self.assertIsNone(
            response_body["extensions"].get("org.matrix.msc4262.profiles"),
        )

        # Set a new field value and ensure our tracking doesn't swallow updates
        self.get_success(
            self.profile_handler.set_field(
                target_user=UserID.from_string(self.other_user),
                requester=create_requester(self.other_user),
                field_name="displayname",
                new_value="new displayname",
            )
        )
        response_body, _ = self.do_sync(sync_body, since=from_token, tok=self.tok)
        self.assertEqual(
            response_body["extensions"]["org.matrix.msc4262.profiles"]["users"][
                "@other_user:test"
            ],
            {
                "updated": {
                    "displayname": "new displayname",
                },
            },
        )

    @override_config({"include_profile_updates_in_sync": True})
    def test_removed_fields_get_sent_down_as_removed(self) -> None:
        """
        > Likewise, any field IDs that are cleared/removed from a user's profile will appear under users-><user_id>->removed.
        > Likewise, the removed field should not be present if there were only updates to existing fields (and none were cleared).
        """

    @override_config({"include_profile_updates_in_sync": True})
    def test_updated_key_only_present_if_updates(self) -> None:
        """
        > The updated field SHOULD only be present if there are changes to existing fields on a user's profile.
        """

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

    @override_config({"include_profile_updates_in_sync": True})
    def test_fields_subset_changing_sends_down_field_even_if_not_changed(self) -> None:
        """
        > Finally, if the list of fields expands to cover a new field ID, those fields should
        > be sent down for all users that are within the current room subset. Future incremental
        > updates will then include changes to this field.
        """

    """
    Sliding Sync offers the lazy_members boolean flag on a per-room basis, which when true
    will only send down membership information for a user if:
    """

    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_down_full_profile_if_events_in_timeline(self) -> None:
        """
        > the user is one of the senders of a timeline event included in the response
        """

    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_down_full_profile_if_membership_events_that_are_returned(
        self,
    ) -> None:
        """
        > users in required_state member events that are returned
        """

    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_down_full_profile_for_heroes(self) -> None:
        """
        > heroes(? - not mentioned in the MSC, but seems like Synapse implements it)
        """

    @override_config({"include_profile_updates_in_sync": True})
    def test_lazy_loading_sends_down_full_profile_for_invite_knock_senders(
        self,
    ) -> None:
        """
        > invite/knock stripped-state users/senders(? - likewise)
        """

    @override_config({"include_profile_updates_in_sync": True})
    def test_repeat_of_sync_correctly_includes_profile_information_again(self) -> None:
        """
        > Homeservers should only consider a profile field update "accepted" by a client
        > once the client returns with a new /sync request with the next /sync token,
        > NOT just after sending down the profile update. The client may never receive
        > response due to network conditions, or a bug in the client implementation.
        """
