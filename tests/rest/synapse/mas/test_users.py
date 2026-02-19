#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from urllib.parse import urlencode

from twisted.internet.testing import MemoryReactor

from synapse.appservice import ApplicationService
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID, create_requester
from synapse.util.clock import Clock

from tests.unittest import skip_unless
from tests.utils import HAS_AUTHLIB

from ._base import BaseTestCase


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasQueryUserResource(BaseTestCase):
    def test_other_token(self) -> None:
        channel = self.make_request(
            "GET",
            "/_synapse/mas/query_user?localpart=alice",
            shorthand=False,
            access_token="other_token",
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_query_user(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )
        self.get_success(
            store.set_profile_avatar_url(
                user_id=alice,
                new_avatar_url="mxc://example.com/avatar",
            )
        )

        channel = self.make_request(
            "GET",
            "/_synapse/mas/query_user?localpart=alice",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body,
            {
                "user_id": "@alice:test",
                "display_name": "Alice",
                "avatar_url": "mxc://example.com/avatar",
                "is_suspended": False,
                "is_deactivated": False,
            },
        )

        self.get_success(
            store.set_user_suspended_status(user_id=str(alice), suspended=True)
        )

        channel = self.make_request(
            "GET",
            "/_synapse/mas/query_user?localpart=alice",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body,
            {
                "user_id": "@alice:test",
                "display_name": "Alice",
                "avatar_url": "mxc://example.com/avatar",
                "is_suspended": True,
                "is_deactivated": False,
            },
        )

        # Deactivate the account, it should clear the display name and avatar
        # and mark the user as deactivated
        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                user_id=str(alice),
                erase_data=True,
                requester=create_requester(alice),
            )
        )

        channel = self.make_request(
            "GET",
            "/_synapse/mas/query_user?localpart=alice",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(
            channel.json_body,
            {
                "user_id": "@alice:test",
                "display_name": None,
                "avatar_url": None,
                "is_suspended": True,
                "is_deactivated": True,
            },
        )

    def test_query_unknown_user(self) -> None:
        channel = self.make_request(
            "GET",
            "/_synapse/mas/query_user?localpart=alice",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 404, channel.json_body)

    def test_query_user_missing_localpart(self) -> None:
        channel = self.make_request(
            "GET",
            "/_synapse/mas/query_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasProvisionUserResource(BaseTestCase):
    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token="other_token",
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_provision_user(self) -> None:
        store = self.hs.get_datastores().main

        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "set_displayname": "Alice",
                "set_emails": ["alice@example.com"],
                "set_avatar_url": "mxc://example.com/avatar",
            },
        )

        # This created the user, hence the 201 status code
        self.assertEqual(channel.code, 201, channel.json_body)
        self.assertEqual(channel.json_body, {})

        alice = UserID("alice", "test")
        profile = self.get_success(store.get_profileinfo(alice))
        self.assertEqual(profile.display_name, "Alice")
        self.assertEqual(profile.avatar_url, "mxc://example.com/avatar")
        threepids = self.get_success(store.user_get_threepids(str(alice)))
        self.assertEqual(len(threepids), 1)
        self.assertEqual(threepids[0].medium, "email")
        self.assertEqual(threepids[0].address, "alice@example.com")

        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "unset_displayname": True,
                "unset_avatar_url": True,
                "unset_emails": True,
            },
        )

        # This updated the user, hence the 200 status code
        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Check that the profile and threepids were deleted
        profile = self.get_success(store.get_profileinfo(alice))
        self.assertEqual(profile.display_name, None)
        self.assertEqual(profile.avatar_url, None)
        threepids = self.get_success(store.user_get_threepids(str(alice)))
        self.assertEqual(threepids, [])

    def test_provision_user_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "set_displayname": "Alice",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_provision_user_empty_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "",
                "set_displayname": "Alice",
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_provision_user_invalid_localpart(self) -> None:
        # Test with characters that are invalid in localparts
        invalid_localparts = [
            "@alice:test",  # That's a MXID
            "alice@domain.com",
            "alice:test",
            "alice space",
            "alice#hash",
            "a" * 1000,  # Very long localpart
        ]

        for localpart in invalid_localparts:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/provision_user",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content={
                    "localpart": localpart,
                    "set_displayname": "Alice",
                },
            )
            # Should be a validation error
            self.assertEqual(
                channel.code, 400, f"Should fail for localpart: {localpart}"
            )

    def test_provision_user_multiple_emails(self) -> None:
        store = self.hs.get_datastores().main

        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "set_emails": ["alice@example.com", "alice.alt@example.com"],
            },
        )

        self.assertEqual(channel.code, 201, channel.json_body)

        alice = UserID("alice", "test")
        threepids = self.get_success(store.user_get_threepids(str(alice)))
        self.assertEqual(len(threepids), 2)
        email_addresses = {tp.address for tp in threepids}
        self.assertEqual(
            email_addresses, {"alice@example.com", "alice.alt@example.com"}
        )

    def test_provision_user_duplicate_emails(self) -> None:
        store = self.hs.get_datastores().main

        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "set_emails": ["alice@example.com", "alice@example.com"],
            },
        )

        self.assertEqual(channel.code, 201, channel.json_body)

        alice = UserID("alice", "test")
        threepids = self.get_success(store.user_get_threepids(str(alice)))
        # Should deduplicate
        self.assertEqual(len(threepids), 1)
        self.assertEqual(threepids[0].address, "alice@example.com")

    def test_provision_user_conflicting_operations(self) -> None:
        # Test setting and unsetting the same field
        channel = self.make_request(
            "POST",
            "/_synapse/mas/provision_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "set_displayname": "Alice",
                "unset_displayname": True,
            },
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_provision_user_invalid_json_types(self) -> None:
        # Test with wrong data types
        invalid_contents: list[JsonDict] = [
            {"localpart": "alice", "set_displayname": 123},  # Number instead of string
            {
                "localpart": "alice",
                "set_emails": "not-an-array",
            },  # String instead of array
            {
                "localpart": "alice",
                "unset_displayname": "not-a-bool",
            },  # String instead of bool
            {"localpart": 123},  # Number instead of string for localpart
        ]

        for content in invalid_contents:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/provision_user",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content=content,
            )
            self.assertEqual(channel.code, 400, f"Should fail for content: {content}")


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasIsLocalpartAvailableResource(BaseTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Provision a user
        store = homeserver.get_datastores().main
        self.get_success(store.register_user("@alice:test"))

    def test_other_token(self) -> None:
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=alice",
            shorthand=False,
            access_token="other_token",
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_is_localpart_available(self) -> None:
        # "alice" is not available
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=alice",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_USER_IN_USE")

        # "bob" is available
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=bob",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

    def test_is_localpart_available_invalid_localparts(self) -> None:
        # Numeric-only localparts are not allowed
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=0",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_INVALID_USERNAME")

        # A super-long MXID is not allowed by the spec
        super_long = "a" * 1000
        channel = self.make_request(
            "GET",
            f"/_synapse/mas/is_localpart_available?localpart={super_long}",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_INVALID_USERNAME")

    def test_is_localpart_available_appservice_exclusive(self) -> None:
        # Insert an appservice which has exclusive namespaces
        appservice = ApplicationService(
            token="i_am_an_app_service",
            id="1234",
            namespaces={"users": [{"regex": r"@as_user_.*:.+", "exclusive": True}]},
            sender=UserID.from_string("@as_main:test"),
        )
        self.hs.get_datastores().main.services_cache = [appservice]

        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=as_main",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_EXCLUSIVE")

        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=as_user_alice",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_EXCLUSIVE")

        # Sanity-check that "bob" is available
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=bob",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

    def test_is_localpart_available_missing_localpart(self) -> None:
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_is_localpart_available_empty_localpart(self) -> None:
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_is_localpart_available_invalid_characters(self) -> None:
        # Test with characters that are invalid in localparts
        invalid_localparts = [
            "alice@domain.com",  # Contains @
            "alice:test",  # Contains :
            "alice space",  # Contains space
            "alice\\backslash",  # Contains backslash
            "alice#hash",  # Contains hash
            "alice$dollar",  # Contains $
            "alice%percent",  # Contains %
            "alice&amp",  # Contains &
            "alice?question",  # Contains ?
            "alice[bracket",  # Contains [
            "alice]bracket",  # Contains ]
            "alice{brace",  # Contains {
            "alice}brace",  # Contains }
            "alice|pipe",  # Contains |
            'alice"quote',  # Contains "
            "alice'apostrophe",  # Contains '
            "alice<less",  # Contains <
            "alice>greater",  # Contains >
            "alice\ttab",  # Contains tab
            "alice\nnewline",  # Contains newline
        ]

        for localpart in invalid_localparts:
            channel = self.make_request(
                "GET",
                f"/_synapse/mas/is_localpart_available?{urlencode({'localpart': localpart})}",
                shorthand=False,
                access_token=self.SHARED_SECRET,
            )
            # Should return 400 for invalid characters
            self.assertEqual(
                channel.code,
                400,
                f"Should reject localpart with invalid chars: {localpart}",
            )
            self.assertEqual(
                channel.json_body["errcode"], "M_INVALID_USERNAME", localpart
            )

    def test_is_localpart_available_case_sensitivity(self) -> None:
        # Register a user with an uppercase localpart
        self.get_success(self.hs.get_datastores().main.register_user("@BOB:test"))

        # It should report as not available, the search should be case-insensitive
        channel = self.make_request(
            "GET",
            "/_synapse/mas/is_localpart_available?localpart=bob",
            shorthand=False,
            access_token=self.SHARED_SECRET,
        )

        self.assertEqual(channel.code, 400, channel.json_body)
        self.assertEqual(channel.json_body["errcode"], "M_USER_IN_USE")


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasDeleteUserResource(BaseTestCase):
    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Provision a user with a display name
        self.get_success(
            homeserver.get_registration_handler().register_user(
                localpart="alice",
                default_display_name="Alice",
            )
        )

    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token="other_token",
            content={"localpart": "alice", "erase": False},
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_delete_user_no_erase(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main

        # Delete the user
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "erase": False},
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Check that the user was deleted
        self.assertTrue(
            self.get_success(store.get_user_deactivated_status(user_id=str(alice)))
        )
        # But not erased
        self.assertFalse(self.get_success(store.is_user_erased(user_id=str(alice))))

    def test_delete_user_erase(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main

        # Delete the user
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "erase": True},
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Check that the user was deleted
        self.assertTrue(
            self.get_success(store.get_user_deactivated_status(user_id=str(alice)))
        )
        # And erased
        self.assertTrue(self.get_success(store.is_user_erased(user_id=str(alice))))

    def test_delete_user_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"erase": False},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_delete_user_missing_erase(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_delete_user_invalid_erase_type(self) -> None:
        invalid_erase_values = [
            "true",  # String instead of bool
            1,  # Number instead of bool
            "false",  # String instead of bool
            0,  # Number instead of bool
            {},  # Object instead of bool
            [],  # Array instead of bool
        ]

        for erase_value in invalid_erase_values:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/delete_user",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content={"localpart": "alice", "erase": erase_value},
            )
            self.assertEqual(
                channel.code, 400, f"Should fail for erase value: {erase_value}"
            )

    def test_delete_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "nonexistent", "erase": False},
        )

        self.assertEqual(channel.code, 404)

    def test_delete_already_deleted_user(self) -> None:
        # First deletion
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "erase": False},
        )
        self.assertEqual(channel.code, 200)

        # Second deletion should be idempotent
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "erase": False},
        )
        self.assertEqual(channel.code, 200)

    def test_delete_user_erase_already_deleted_user(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main

        # First delete without erase
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "erase": False},
        )
        self.assertEqual(channel.code, 200)

        # Verify not erased initially
        self.assertFalse(self.get_success(store.is_user_erased(user_id=str(alice))))

        # Now delete with erase
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "erase": True},
        )
        self.assertEqual(channel.code, 200)

        # Should now be erased
        self.assertTrue(self.get_success(store.is_user_erased(user_id=str(alice))))

    def test_delete_user_empty_json(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_delete_user_extra_fields(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/delete_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "erase": False,
                "extra_field": "should_be_ignored",
                "another_field": 123,
            },
        )

        # Should succeed and ignore extra fields
        self.assertEqual(channel.code, 200, channel.json_body)


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasReactivateUserResource(BaseTestCase):
    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token="other_token",
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_reactivate_user(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )
        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                user_id=str(alice),
                erase_data=True,
                requester=create_requester(alice),
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Check that the user was reactivated
        self.assertFalse(
            self.get_success(store.get_user_deactivated_status(user_id=str(alice)))
        )

    def test_reactivate_user_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_reactivate_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "nonexistent"},
        )

        self.assertEqual(channel.code, 404, channel.json_body)

    def test_reactivate_active_user(self) -> None:
        # Create an active user
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        # Should be idempotent
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_reactivate_erased_user(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )

        # Deactivate with erase
        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                user_id=str(alice),
                erase_data=True,
                requester=create_requester(alice),
            )
        )

        # Verify user is erased
        self.assertTrue(self.get_success(store.is_user_erased(user_id=str(alice))))

        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        # Should succeed even for erased users
        self.assertEqual(channel.code, 200, channel.json_body)
        # Shouldn't be erased anymore
        self.assertFalse(self.get_success(store.is_user_erased(user_id=str(alice))))

    def test_reactivate_user_extra_fields(self) -> None:
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
            )
        )
        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                user_id=str(alice),
                erase_data=False,
                requester=create_requester(alice),
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/reactivate_user",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "extra_field": "should_be_ignored",
                "another_field": 123,
            },
        )

        # Should succeed and ignore extra fields
        self.assertEqual(channel.code, 200, channel.json_body)


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasSetDisplayNameResource(BaseTestCase):
    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/set_displayname",
            shorthand=False,
            access_token="other_token",
            content={"localpart": "alice", "displayname": "Bob"},
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_set_display_name(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )
        profile = self.get_success(store.get_profileinfo(alice))
        self.assertEqual(profile.display_name, "Alice")

        channel = self.make_request(
            "POST",
            "/_synapse/mas/set_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "displayname": "Bob"},
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Check that the profile was updated
        profile = self.get_success(store.get_profileinfo(alice))
        self.assertEqual(profile.display_name, "Bob")

    def test_set_display_name_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/set_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"displayname": "Bob"},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_set_display_name_missing_displayname(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/set_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_set_display_name_very_long(self) -> None:
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
            )
        )

        long_name = "A" * 1000
        channel = self.make_request(
            "POST",
            "/_synapse/mas/set_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice", "displayname": long_name},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_set_display_name_special_characters(self) -> None:
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
            )
        )

        special_names = [
            "Alice 👋",  # Emoji
            "Alice & Bob",  # HTML entities
            "Alice\nNewline",  # Newline
            "Alice\tTab",  # Tab
            'Alice"Quote',  # Quote
            "Alice'Apostrophe",  # Apostrophe
            "Alice<script>",  # HTML tags
        ]

        for name in special_names:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/set_displayname",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content={"localpart": "alice", "displayname": name},
            )

            # Should handle special characters gracefully
            self.assertEqual(channel.code, 200, f"Failed for name: {repr(name)}")

    def test_set_display_name_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/set_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "nonexistent", "displayname": "Bob"},
        )

        # Should fail for non-existent user
        self.assertEqual(channel.code, 404, channel.json_body)

    def test_set_display_name_invalid_json_type(self) -> None:
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
            )
        )

        invalid_types = [
            123,  # Number
            True,  # Boolean
            [],  # Array
            {},  # Object
            None,  # Null
        ]

        for invalid_displayname in invalid_types:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/set_displayname",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content={"localpart": "alice", "displayname": invalid_displayname},
            )
            self.assertEqual(
                channel.code, 400, f"Should fail for type: {type(invalid_displayname)}"
            )


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasUnsetDisplayNameResource(BaseTestCase):
    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token="other_token",
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_unset_display_name(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Check that the profile was updated
        profile = self.get_success(store.get_profileinfo(alice))
        self.assertEqual(profile.display_name, None)

    def test_unset_display_name_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_unset_display_name_idempotent(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 200, channel.json_body)

        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        # Should be idempotent
        self.assertEqual(channel.code, 200, channel.json_body)

        # Still should be None
        profile = self.get_success(store.get_profileinfo(alice))
        self.assertEqual(profile.display_name, None)

    def test_unset_display_name_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "nonexistent"},
        )

        # Should fail for non-existent user
        self.assertEqual(channel.code, 404, channel.json_body)

    def test_unset_display_name_extra_fields(self) -> None:
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(
                localpart=alice.localpart,
                default_display_name="Alice",
            )
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/unset_displayname",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "extra_field": "should_be_ignored",
                "another_field": 123,
            },
        )

        # Should succeed and ignore extra fields
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_unset_display_name_invalid_localpart_type(self) -> None:
        invalid_types = [
            123,  # Number
            True,  # Boolean
            [],  # Array
            {},  # Object
            None,  # Null
        ]

        for invalid_localpart in invalid_types:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/unset_displayname",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content={"localpart": invalid_localpart},
            )
            self.assertEqual(
                channel.code, 400, f"Should fail for type: {type(invalid_localpart)}"
            )


@skip_unless(HAS_AUTHLIB, "requires authlib")
class MasAllowCrossSigningResetResource(BaseTestCase):
    def test_other_token(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token="other_token",
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 403, channel.json_body)
        self.assertEqual(
            channel.json_body["error"], "This endpoint must only be called by MAS"
        )

    def test_allow_cross_signing_reset(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        handler = self.hs.get_e2e_keys_handler()
        self.get_success(
            self.hs.get_registration_handler().register_user(localpart=alice.localpart)
        )

        # Upload a master key
        dummy_key = {"keys": {"a": "b"}}
        self.get_success(
            store.set_e2e_cross_signing_key(str(alice), "master", dummy_key)
        )

        # The key exists but is not replaceable without UIA
        exists, replaceable_without_uia = self.get_success(
            handler.check_cross_signing_setup(user_id=str(alice))
        )
        self.assertIs(exists, True)
        self.assertIs(replaceable_without_uia, False)

        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        self.assertEqual(channel.code, 200, channel.json_body)
        self.assertEqual(channel.json_body, {})

        # Now it should be replaceable without UIA
        exists, replaceable_without_uia = self.get_success(
            handler.check_cross_signing_setup(user_id=str(alice))
        )
        self.assertIs(exists, True)
        self.assertIs(replaceable_without_uia, True)

    def test_allow_cross_signing_reset_missing_localpart(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={},
        )

        self.assertEqual(channel.code, 400, channel.json_body)

    def test_allow_cross_signing_reset_nonexistent_user(self) -> None:
        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "nonexistent"},
        )

        self.assertEqual(channel.code, 404, channel.json_body)

    def test_allow_cross_signing_reset_no_keys(self) -> None:
        # Test with a user that has no cross-signing keys
        alice = UserID("alice", "test")
        handler = self.hs.get_e2e_keys_handler()
        self.get_success(
            self.hs.get_registration_handler().register_user(localpart=alice.localpart)
        )

        # Verify no keys exist
        exists, replaceable_without_uia = self.get_success(
            handler.check_cross_signing_setup(user_id=str(alice))
        )
        self.assertIs(exists, False)

        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )

        # Should succeed even with no existing keys
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_allow_cross_signing_reset_already_replaceable(self) -> None:
        alice = UserID("alice", "test")
        store = self.hs.get_datastores().main
        handler = self.hs.get_e2e_keys_handler()
        self.get_success(
            self.hs.get_registration_handler().register_user(localpart=alice.localpart)
        )

        # Upload a master key
        dummy_key = {"keys": {"a": "b"}}
        self.get_success(
            store.set_e2e_cross_signing_key(str(alice), "master", dummy_key)
        )

        # First call to make it replaceable
        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )
        self.assertEqual(channel.code, 200)

        # Verify it's now replaceable
        exists, replaceable_without_uia = self.get_success(
            handler.check_cross_signing_setup(user_id=str(alice))
        )
        self.assertIs(exists, True)
        self.assertIs(replaceable_without_uia, True)

        # Second call should be idempotent
        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={"localpart": "alice"},
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_allow_cross_signing_reset_extra_fields(self) -> None:
        alice = UserID("alice", "test")
        self.get_success(
            self.hs.get_registration_handler().register_user(localpart=alice.localpart)
        )

        channel = self.make_request(
            "POST",
            "/_synapse/mas/allow_cross_signing_reset",
            shorthand=False,
            access_token=self.SHARED_SECRET,
            content={
                "localpart": "alice",
                "extra_field": "should_be_ignored",
                "another_field": 123,
            },
        )

        # Should succeed and ignore extra fields
        self.assertEqual(channel.code, 200, channel.json_body)

    def test_allow_cross_signing_reset_invalid_localpart_type(self) -> None:
        invalid_types = [
            123,  # Number
            True,  # Boolean
            [],  # Array
            {},  # Object
            None,  # Null
        ]

        for invalid_localpart in invalid_types:
            channel = self.make_request(
                "POST",
                "/_synapse/mas/allow_cross_signing_reset",
                shorthand=False,
                access_token=self.SHARED_SECRET,
                content={"localpart": invalid_localpart},
            )
            self.assertEqual(
                channel.code, 400, f"Should fail for type: {type(invalid_localpart)}"
            )
