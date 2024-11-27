#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2020 The Matrix.org Foundation C.I.C.
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

from typing import Any, Dict, Optional, Set, Tuple
from unittest.mock import AsyncMock, Mock

import attr

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.errors import RedirectException
from synapse.module_api import ModuleApi
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.unittest import HomeserverTestCase, override_config

# Check if we have the dependencies to run the tests.
try:
    import saml2.config
    import saml2.response
    from saml2.sigver import SigverError

    has_saml2 = True

    # pysaml2 can be installed and imported, but might not be able to find xmlsec1.
    config = saml2.config.SPConfig()
    try:
        config.load({"metadata": {}})
        has_xmlsec1 = True
    except SigverError:
        has_xmlsec1 = False
except ImportError:
    has_saml2 = False
    has_xmlsec1 = False

# These are a few constants that are used as config parameters in the tests.
BASE_URL = "https://synapse/"


@attr.s
class FakeAuthnResponse:
    ava = attr.ib(type=dict)
    assertions = attr.ib(type=list, factory=list)
    in_response_to = attr.ib(type=Optional[str], default=None)


class TestMappingProvider:
    def __init__(self, config: None, module: ModuleApi):
        pass

    @staticmethod
    def parse_config(config: JsonDict) -> None:
        return None

    @staticmethod
    def get_saml_attributes(config: None) -> Tuple[Set[str], Set[str]]:
        return {"uid"}, {"displayName"}

    def get_remote_user_id(
        self, saml_response: "saml2.response.AuthnResponse", client_redirect_url: str
    ) -> str:
        return saml_response.ava["uid"]

    def saml_response_to_user_attributes(
        self,
        saml_response: "saml2.response.AuthnResponse",
        failures: int,
        client_redirect_url: str,
    ) -> dict:
        localpart = saml_response.ava["username"] + (str(failures) if failures else "")
        return {"mxid_localpart": localpart, "displayname": None}


class TestRedirectMappingProvider(TestMappingProvider):
    def saml_response_to_user_attributes(
        self,
        saml_response: "saml2.response.AuthnResponse",
        failures: int,
        client_redirect_url: str,
    ) -> dict:
        raise RedirectException(b"https://custom-saml-redirect/")


class SamlHandlerTestCase(HomeserverTestCase):
    def default_config(self) -> Dict[str, Any]:
        config = super().default_config()
        config["public_baseurl"] = BASE_URL
        saml_config: Dict[str, Any] = {
            "sp_config": {"metadata": {}},
            # Disable grandfathering.
            "grandfathered_mxid_source_attribute": None,
            "user_mapping_provider": {"module": __name__ + ".TestMappingProvider"},
        }

        # Update this config with what's in the default config so that
        # override_config works as expected.
        saml_config.update(config.get("saml2_config", {}))
        config["saml2_config"] = saml_config

        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()

        self.handler = hs.get_saml_handler()

        # Reduce the number of attempts when generating MXIDs.
        sso_handler = hs.get_sso_handler()
        sso_handler._MAP_USERNAME_RETRIES = 3

        return hs

    if not has_saml2:
        skip = "Requires pysaml2"
    elif not has_xmlsec1:
        skip = "Requires xmlsec1"

    def test_map_saml_response_to_user(self) -> None:
        """Ensure that mapping the SAML response returned from a provider to an MXID works properly."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # send a mocked-up SAML response to the callback
        saml_response = FakeAuthnResponse({"uid": "test_user", "username": "test_user"})
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "redirect_uri")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "saml",
            request,
            "redirect_uri",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )

    @override_config({"saml2_config": {"grandfathered_mxid_source_attribute": "mxid"}})
    def test_map_saml_response_to_existing_user(self) -> None:
        """Existing users can log in with SAML account."""
        store = self.hs.get_datastores().main
        self.get_success(
            store.register_user(user_id="@test_user:test", password_hash=None)
        )

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # Map a user via SSO.
        saml_response = FakeAuthnResponse(
            {"uid": "tester", "mxid": ["test_user"], "username": "test_user"}
        )
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "saml",
            request,
            "",
            None,
            new_user=False,
            auth_provider_session_id=None,
        )

        # Subsequent calls should map to the same mxid.
        auth_handler.complete_sso_login.reset_mock()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "")
        )
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "saml",
            request,
            "",
            None,
            new_user=False,
            auth_provider_session_id=None,
        )

    def test_map_saml_response_to_invalid_localpart(self) -> None:
        """If the mapping provider generates an invalid localpart it should be rejected."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # mock out the error renderer too
        sso_handler = self.hs.get_sso_handler()
        sso_handler.render_error = Mock(return_value=None)  # type: ignore[method-assign]

        saml_response = FakeAuthnResponse({"uid": "test", "username": "föö"})
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, ""),
        )
        sso_handler.render_error.assert_called_once_with(
            request, "mapping_error", "localpart is invalid: föö"
        )
        auth_handler.complete_sso_login.assert_not_called()

    def test_map_saml_response_to_user_retries(self) -> None:
        """The mapping provider can retry generating an MXID if the MXID is already in use."""

        # stub out the auth handler and error renderer
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]
        sso_handler = self.hs.get_sso_handler()
        sso_handler.render_error = Mock(return_value=None)  # type: ignore[method-assign]

        # register a user to occupy the first-choice MXID
        store = self.hs.get_datastores().main
        self.get_success(
            store.register_user(user_id="@test_user:test", password_hash=None)
        )

        # send the fake SAML response
        saml_response = FakeAuthnResponse({"uid": "test", "username": "test_user"})
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, ""),
        )

        # test_user is already taken, so test_user1 gets registered instead.
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user1:test",
            "saml",
            request,
            "",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )
        auth_handler.complete_sso_login.reset_mock()

        # Register all of the potential mxids for a particular SAML username.
        self.get_success(
            store.register_user(user_id="@tester:test", password_hash=None)
        )
        for i in range(1, 3):
            self.get_success(
                store.register_user(user_id="@tester%d:test" % i, password_hash=None)
            )

        # Now attempt to map to a username, this will fail since all potential usernames are taken.
        saml_response = FakeAuthnResponse({"uid": "tester", "username": "tester"})
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, ""),
        )
        sso_handler.render_error.assert_called_once_with(
            request,
            "mapping_error",
            "Unable to generate a Matrix ID from the SSO response",
        )
        auth_handler.complete_sso_login.assert_not_called()

    @override_config(
        {
            "saml2_config": {
                "user_mapping_provider": {
                    "module": __name__ + ".TestRedirectMappingProvider"
                },
            }
        }
    )
    def test_map_saml_response_redirect(self) -> None:
        """Test a mapping provider that raises a RedirectException"""

        saml_response = FakeAuthnResponse({"uid": "test", "username": "test_user"})
        request = _mock_request()
        e = self.get_failure(
            self.handler._handle_authn_response(request, saml_response, ""),
            RedirectException,
        )
        self.assertEqual(e.value.location, b"https://custom-saml-redirect/")

    @override_config(
        {
            "saml2_config": {
                "attribute_requirements": [
                    {"attribute": "userGroup", "value": "staff"},
                    {"attribute": "department", "value": "sales"},
                ],
            },
        }
    )
    def test_attribute_requirements(self) -> None:
        """The required attributes must be met from the SAML response."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # The response doesn't have the proper userGroup or department.
        saml_response = FakeAuthnResponse({"uid": "test_user", "username": "test_user"})
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "redirect_uri")
        )
        auth_handler.complete_sso_login.assert_not_called()

        # The response doesn't have the proper department.
        saml_response = FakeAuthnResponse(
            {"uid": "test_user", "username": "test_user", "userGroup": ["staff"]}
        )
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "redirect_uri")
        )
        auth_handler.complete_sso_login.assert_not_called()

        # Add the proper attributes and it should succeed.
        saml_response = FakeAuthnResponse(
            {
                "uid": "test_user",
                "username": "test_user",
                "userGroup": ["staff", "admin"],
                "department": ["sales"],
            }
        )
        request.reset_mock()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "redirect_uri")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "saml",
            request,
            "redirect_uri",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )

    @override_config(
        {
            "saml2_config": {
                "attribute_requirements": [
                    {"attribute": "userGroup", "one_of": ["staff", "admin"]},
                ],
            },
        }
    )
    def test_attribute_requirements_one_of(self) -> None:
        """The required attributes can be comma-separated."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # The response doesn't have the proper department.
        saml_response = FakeAuthnResponse(
            {"uid": "test_user", "username": "test_user", "userGroup": ["nogroup"]}
        )
        request = _mock_request()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "redirect_uri")
        )
        auth_handler.complete_sso_login.assert_not_called()

        # Add the proper attributes and it should succeed.
        saml_response = FakeAuthnResponse(
            {"uid": "test_user", "username": "test_user", "userGroup": ["admin"]}
        )
        request.reset_mock()
        self.get_success(
            self.handler._handle_authn_response(request, saml_response, "redirect_uri")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "saml",
            request,
            "redirect_uri",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )


def _mock_request() -> Mock:
    """Returns a mock which will stand in as a SynapseRequest"""
    mock = Mock(
        spec=[
            "finish",
            "getClientAddress",
            "getHeader",
            "setHeader",
            "setResponseCode",
            "write",
        ]
    )
    # `_disconnected` musn't be another `Mock`, otherwise it will be truthy.
    mock._disconnected = False
    return mock
