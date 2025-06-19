from unittest import mock
from unittest.mock import Mock

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
import synapse.rest.scim
from synapse.config import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.rest.client import login
from synapse.rest.scim import HAS_SCIM2, SCIM_DEFAULT_IDP_ID
from synapse.server import HomeServer
from synapse.types import JsonDict, UserID
from synapse.util import Clock

from tests.handlers.test_profile import mock_get_file
from tests.unittest import HomeserverTestCase, skip_unless
from tests.utils import default_config


@skip_unless(HAS_SCIM2, "requires scim2-models")
class SCIMExperimentalFeatureTestCase(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        synapse.rest.scim.register_scim_servlets,
        login.register_servlets,
    ]
    url = "/_synapse/admin/scim/v2"

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.admin_user_id = self.register_user(
            "admin", "pass", admin=True, displayname="admin display name"
        )
        self.admin_user_tok = self.login("admin", "pass")
        self.user_user_id = self.register_user(
            "user", "pass", admin=False, displayname="user display name"
        )

    def test_disabled_by_default(self) -> None:
        """
        Without explicitly enabled by configuration, the SCIM API endpoint should be
        disabled.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users/{self.user_user_id}",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(404, channel.code, msg=channel.json_body)

    def test_exclusive_with_msc3861(self) -> None:
        """
        Without explicitly enabled by configuration, the SCIM API endpoint should be
        disabled.
        """

        config_dict = {
            "experimental_features": {
                "scim": {
                    "enabled": True,
                },
                "msc3861": {"enabled": True},
            },
            **default_config("test"),
        }

        with self.assertRaises(ConfigError):
            config = HomeServerConfig()
            config.parse_config_dict(config_dict, "", "")


@skip_unless(HAS_SCIM2, "requires scim2-models")
class UserProvisioningTestCase(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        synapse.rest.scim.register_scim_servlets,
        login.register_servlets,
    ]
    url = "/_synapse/admin/scim/v2"

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.http_client = Mock(spec=["get_file"])
        self.http_client.get_file.side_effect = mock_get_file
        self.http_client.user_agent = b"Synapse Test"

        hs = self.setup_test_homeserver(
            proxied_blocklisted_http_client=self.http_client,
        )
        return hs

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        scim_conf = conf.setdefault("experimental_features", {}).setdefault("scim", {})
        scim_conf.setdefault("enabled", True)
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.admin_user_id = self.register_user(
            "admin", "pass", admin=True, displayname="admin display name"
        )
        self.admin_user_tok = self.login("admin", "pass")
        self.user_user_id = self.register_user(
            "user", "pass", admin=False, displayname="user display name"
        )
        self.other_user_ids = [
            self.register_user(f"user{i:02d}", "pass", displayname=f"user{i}")
            for i in range(15)
        ]
        self.get_success(
            self.store.user_add_threepid(
                self.user_user_id, "email", "user@mydomain.tld", 0, 0
            )
        )
        self.get_success(
            self.store.user_add_threepid(
                self.user_user_id, "msisdn", "+1-12345678", 1, 1
            )
        )
        self.get_success(
            self.store.set_profile_avatar_url(
                UserID.from_string(self.user_user_id),
                "mxc://servername/mediaid",
            )
        )
        self.get_success(
            self.store.record_user_external_id(
                SCIM_DEFAULT_IDP_ID, "IDP-user", self.user_user_id
            )
        )

    def test_get_user(self) -> None:
        """
        Nominal test of the /Users/<user_id> endpoint.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users/{self.user_user_id}",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "meta": {
                    "resourceType": "User",
                    "created": mock.ANY,
                    "lastModified": mock.ANY,
                    "location": "https://test/_synapse/admin/scim/v2/Users/@user:test",
                },
                "id": "@user:test",
                "userName": "user",
                "externalId": "IDP-user",
                "phoneNumbers": [{"value": "+1-12345678"}],
                "emails": [{"value": "user@mydomain.tld"}],
                "active": True,
                "displayName": "user display name",
                "photos": [
                    {
                        "type": "photo",
                        "primary": True,
                        "value": "https://test/_matrix/media/v1/thumbnail/servername/mediaid",
                    }
                ],
            },
            channel.json_body,
        )
        self.assertIn(
            b"application/scim+json",
            channel.headers.getRawHeaders(b"Content-Type") or [],
        )

    def test_get_user_include_attribute(self) -> None:
        """
        Nominal test of the /Users/<user_id> endpoint with attribute inclusion arguments.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users/{self.user_user_id}?attributes=userName",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "id": "@user:test",
                "userName": "user",
            },
            channel.json_body,
        )

    def test_get_user_exclude_attribute(self) -> None:
        """
        Nominal test of the /Users/<user_id> endpoint with attribute exclusion arguments.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users/{self.user_user_id}?excludedAttributes=userName",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "meta": {
                    "resourceType": "User",
                    "created": mock.ANY,
                    "lastModified": mock.ANY,
                    "location": "https://test/_synapse/admin/scim/v2/Users/@user:test",
                },
                "id": "@user:test",
                "externalId": "IDP-user",
                "phoneNumbers": [{"value": "+1-12345678"}],
                "emails": [{"value": "user@mydomain.tld"}],
                "active": True,
                "displayName": "user display name",
                "photos": [
                    {
                        "type": "photo",
                        "primary": True,
                        "value": "https://test/_matrix/media/v1/thumbnail/servername/mediaid",
                    }
                ],
            },
            channel.json_body,
        )

    def test_get_users(self) -> None:
        """
        Nominal test of the /Users endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        )
        self.assertEqual(len(channel.json_body["Resources"]), 17)

        self.assertTrue(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "meta": {
                    "resourceType": "User",
                    "created": mock.ANY,
                    "lastModified": mock.ANY,
                    "location": "https://test/_synapse/admin/scim/v2/Users/@user:test",
                },
                "id": "@user:test",
                "userName": "user",
                "externalId": "IDP-user",
                "phoneNumbers": [{"value": "+1-12345678"}],
                "emails": [{"value": "user@mydomain.tld"}],
                "active": True,
                "displayName": "user display name",
                "photos": [
                    {
                        "type": "photo",
                        "primary": True,
                        "value": "https://test/_matrix/media/v1/thumbnail/servername/mediaid",
                    }
                ],
            }
            in channel.json_body["Resources"],
        )

    def test_get_users_pagination_count(self) -> None:
        """
        Test the 'count' parameter of the /Users endpoint.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users?count=2",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        )
        self.assertEqual(len(channel.json_body["Resources"]), 2)

    def test_get_users_pagination_start_index(self) -> None:
        """
        Test the 'startIndex' parameter of the /Users endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users?startIndex=2&count=1",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        )
        self.assertEqual(len(channel.json_body["Resources"]), 1)
        self.assertEqual(channel.json_body["Resources"][0]["id"], "@user00:test")

    def test_get_users_pagination_big_start_index(self) -> None:
        """
        Test the 'startIndex' parameter of the /Users endpoint
        is not greater than the number of users.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users?startIndex=1234",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        )
        self.assertEqual(
            0,
            len(channel.json_body["Resources"]),
        )
        self.assertEqual(
            17,
            channel.json_body["totalResults"],
        )

    def test_get_invalid_user(self) -> None:
        """
        Attempt to retrieve user information with a wrong username.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users/@bjensen:test",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(
            ["urn:ietf:params:scim:api:messages:2.0:Error"],
            channel.json_body["schemas"],
        )

    def test_post_user(self) -> None:
        """
        Create a new user.
        """
        request_data: JsonDict = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "bjensen",
            "externalId": "IDP-bjensen",
            "phoneNumbers": [{"value": "+1-12345678"}],
            "emails": [{"value": "bjensen@mydomain.tld"}],
            "photos": [
                {
                    "type": "photo",
                    "primary": True,
                    "value": "http://my.server/me.png",
                }
            ],
            "active": True,
            "displayName": "bjensen display name",
            "password": "correct horse battery staple",
        }
        channel = self.make_request(
            "POST",
            f"{self.url}/Users/",
            request_data,
            access_token=self.admin_user_tok,
        )
        self.assertEqual(201, channel.code, msg=channel.json_body)

        expected = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "meta": {
                "resourceType": "User",
                "created": mock.ANY,
                "lastModified": mock.ANY,
                "location": "https://test/_synapse/admin/scim/v2/Users/@bjensen:test",
            },
            "id": "@bjensen:test",
            "externalId": "IDP-bjensen",
            "phoneNumbers": [{"value": "+1-12345678"}],
            "userName": "bjensen",
            "emails": [{"value": "bjensen@mydomain.tld"}],
            "active": True,
            "photos": [
                {
                    "type": "photo",
                    "primary": True,
                    "value": mock.ANY,
                }
            ],
            "displayName": "bjensen display name",
        }
        self.assertEqual(expected, channel.json_body)
        self.assertSubstring(
            "https://test/_matrix/media/v1/thumbnail/test/",
            channel.json_body["photos"][0]["value"],
        )

        channel = self.make_request(
            "GET",
            f"{self.url}/Users/@bjensen:test",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(expected, channel.json_body)

    def test_delete_user(self) -> None:
        """
        Delete an existing user.
        """
        channel = self.make_request(
            "DELETE",
            f"{self.url}/Users/@user:test",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(204, channel.code)
        self.assertTrue(self.store.is_user_erased("@user:test"))

    def test_delete_invalid_user(self) -> None:
        """
        Attempt to delete a user with a non-existing username.
        """

        channel = self.make_request(
            "GET",
            f"{self.url}/Users/@bjensen:test",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(404, channel.code)
        self.assertEqual(
            ["urn:ietf:params:scim:api:messages:2.0:Error"],
            channel.json_body["schemas"],
        )

    def test_replace_user(self) -> None:
        """
        Replace user information.
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Users/@user:test",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
                "meta": {
                    "resourceType": "User",
                    "created": mock.ANY,
                    "lastModified": mock.ANY,
                    "location": "https://test/_synapse/admin/scim/v2/Users/@user:test",
                },
                "id": "@user:test",
                "userName": "user",
                "externalId": "IDP-user",
                "phoneNumbers": [{"value": "+1-12345678"}],
                "emails": [{"value": "user@mydomain.tld"}],
                "photos": [
                    {
                        "type": "photo",
                        "primary": True,
                        "value": "https://test/_matrix/media/v1/thumbnail/servername/mediaid",
                    }
                ],
                "active": True,
                "displayName": "user display name",
            },
            channel.json_body,
        )

        request_data: JsonDict = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "externalId": "IDP-user",
            "phoneNumbers": [{"value": "+1-11112222"}],
            "emails": [{"value": "newmail@mydomain.tld"}],
            "userName": "user",
            "displayName": "new display name",
            "photos": [
                {
                    "type": "photo",
                    "primary": True,
                    "value": "http://my.server/me.png",
                }
            ],
        }

        channel = self.make_request(
            "PUT",
            f"{self.url}/Users/@user:test",
            request_data,
            access_token=self.admin_user_tok,
        )
        self.assertEqual(200, channel.code)

        expected = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "meta": {
                "resourceType": "User",
                "created": mock.ANY,
                "lastModified": mock.ANY,
                "location": "https://test/_synapse/admin/scim/v2/Users/@user:test",
            },
            "id": "@user:test",
            "externalId": "IDP-user",
            "phoneNumbers": [{"value": "+1-11112222"}],
            "userName": "user",
            "emails": [{"value": "newmail@mydomain.tld"}],
            "active": True,
            "displayName": "new display name",
            "photos": [
                {
                    "type": "photo",
                    "primary": True,
                    "value": mock.ANY,
                }
            ],
        }
        self.assertEqual(expected, channel.json_body)
        self.assertSubstring(
            "https://test/_matrix/media/v1/thumbnail/test/",
            channel.json_body["photos"][0]["value"],
        )

        channel = self.make_request(
            "GET",
            f"{self.url}/Users/@user:test",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(expected, channel.json_body)

    def test_replace_invalid_user(self) -> None:
        """
        Attempt to replace user information based on a wrong username.
        """
        request_data: JsonDict = {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "phoneNumbers": [{"value": "+1-11112222"}],
            "emails": [{"value": "newmail@mydomain.tld"}],
            "userName": "user",
            "displayName": "new display name",
            "photos": [
                {
                    "type": "photo",
                    "primary": True,
                    "value": "http://my.server/me.png",
                }
            ],
        }

        channel = self.make_request(
            "PUT",
            f"{self.url}/Users/@bjensen:test",
            request_data,
            access_token=self.admin_user_tok,
        )
        self.assertEqual(404, channel.code)
        self.assertEqual(
            ["urn:ietf:params:scim:api:messages:2.0:Error"],
            channel.json_body["schemas"],
        )


@skip_unless(HAS_SCIM2, "requires scim2-models")
class SCIMMetadataTestCase(HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        synapse.rest.scim.register_scim_servlets,
        login.register_servlets,
    ]
    url = "/_synapse/admin/scim/v2"

    def default_config(self) -> JsonDict:
        conf = super().default_config()
        scim_conf = conf.setdefault("experimental_features", {}).setdefault("scim", {})
        scim_conf.setdefault("enabled", True)
        return conf

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

        self.admin_user_id = self.register_user(
            "admin", "pass", admin=True, displayname="admin display name"
        )
        self.admin_user_tok = self.login("admin", "pass")
        self.schemas = [
            "urn:ietf:params:scim:schemas:core:2.0:User",
            "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig",
            "urn:ietf:params:scim:schemas:core:2.0:Schema",
            "urn:ietf:params:scim:schemas:core:2.0:ResourceType",
        ]

    def test_get_schemas(self) -> None:
        """
        Read the /Schemas endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Schemas",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        )

        for schema in self.schemas:
            self.assertTrue(
                any(item["id"] == schema for item in channel.json_body["Resources"])
            )

    def test_get_schema(self) -> None:
        """
        Read the /Schemas/<schema-id> endpoint
        """
        for schema in self.schemas:
            channel = self.make_request(
                "GET",
                f"{self.url}/Schemas/{schema}",
                access_token=self.admin_user_tok,
            )
            self.assertEqual(200, channel.code, msg=channel.json_body)
            self.assertEqual(channel.json_body["id"], schema)

    def test_get_invalid_schema(self) -> None:
        """
        Read the /Schemas endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/Schemas/urn:ietf:params:scim:schemas:core:2.0:Group",
            access_token=self.admin_user_tok,
        )
        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(
            ["urn:ietf:params:scim:api:messages:2.0:Error"],
            channel.json_body["schemas"],
        )

    def test_get_service_provider_config(self) -> None:
        """
        Read the /ServiceProviderConfig endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/ServiceProviderConfig",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        )

    def test_get_resource_types(self) -> None:
        """
        Read the /ResourceTypes endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/ResourceTypes",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
        )

    def test_get_resource_type_user(self) -> None:
        """
        Read the /ResourceTypes/User endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/ResourceTypes/User",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(200, channel.code, msg=channel.json_body)
        self.assertEqual(
            channel.json_body["schemas"],
            ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        )

    def test_get_invalid_resource_type(self) -> None:
        """
        Read an invalid /ResourceTypes/ endpoint
        """
        channel = self.make_request(
            "GET",
            f"{self.url}/ResourceTypes/Group",
            access_token=self.admin_user_tok,
        )

        self.assertEqual(404, channel.code, msg=channel.json_body)
        self.assertEqual(
            ["urn:ietf:params:scim:api:messages:2.0:Error"],
            channel.json_body["schemas"],
        )
