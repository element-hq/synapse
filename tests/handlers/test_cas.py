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
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

from twisted.internet.testing import MemoryReactor

from synapse.handlers.cas import CasResponse
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase, override_config

# These are a few constants that are used as config parameters in the tests.
BASE_URL = "https://synapse/"
SERVER_URL = "https://issuer/"


class CasHandlerTestCase(HomeserverTestCase):
    def default_config(self) -> Dict[str, Any]:
        config = super().default_config()
        config["public_baseurl"] = BASE_URL
        cas_config = {
            "enabled": True,
            "server_url": SERVER_URL,
            "service_url": BASE_URL,
        }

        # Update this config with what's in the default config so that
        # override_config works as expected.
        cas_config.update(config.get("cas_config", {}))
        config["cas_config"] = cas_config

        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()

        self.handler = hs.get_cas_handler()

        # Reduce the number of attempts when generating MXIDs.
        sso_handler = hs.get_sso_handler()
        sso_handler._MAP_USERNAME_RETRIES = 3

        return hs

    def test_map_cas_user_to_user(self) -> None:
        """Ensure that mapping the CAS user returned from a provider to an MXID works properly."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        cas_response = CasResponse("test_user", {})
        request = _mock_request()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "cas",
            request,
            "redirect_uri",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )

    def test_map_cas_user_to_existing_user(self) -> None:
        """Existing users can log in with CAS account."""
        store = self.hs.get_datastores().main
        self.get_success(
            store.register_user(user_id="@test_user:test", password_hash=None)
        )

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # Map a user via SSO.
        cas_response = CasResponse("test_user", {})
        request = _mock_request()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "cas",
            request,
            "redirect_uri",
            None,
            new_user=False,
            auth_provider_session_id=None,
        )

        # Subsequent calls should map to the same mxid.
        auth_handler.complete_sso_login.reset_mock()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "cas",
            request,
            "redirect_uri",
            None,
            new_user=False,
            auth_provider_session_id=None,
        )

    def test_map_cas_user_to_invalid_localpart(self) -> None:
        """CAS automaps invalid characters to base-64 encoding."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        cas_response = CasResponse("föö", {})
        request = _mock_request()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@f=c3=b6=c3=b6:test",
            "cas",
            request,
            "redirect_uri",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )

    @override_config(
        {
            "cas_config": {
                "required_attributes": {"userGroup": "staff", "department": None}
            }
        }
    )
    def test_required_attributes(self) -> None:
        """The required attributes must be met from the CAS response."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        # The response doesn't have the proper userGroup or department.
        cas_response = CasResponse("test_user", {})
        request = _mock_request()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )
        auth_handler.complete_sso_login.assert_not_called()

        # The response doesn't have any department.
        cas_response = CasResponse("test_user", {"userGroup": ["staff"]})
        request.reset_mock()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )
        auth_handler.complete_sso_login.assert_not_called()

        # Add the proper attributes and it should succeed.
        cas_response = CasResponse(
            "test_user", {"userGroup": ["staff", "admin"], "department": ["sales"]}
        )
        request.reset_mock()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )

        # check that the auth handler got called as expected
        auth_handler.complete_sso_login.assert_called_once_with(
            "@test_user:test",
            "cas",
            request,
            "redirect_uri",
            None,
            new_user=True,
            auth_provider_session_id=None,
        )

    @override_config({"cas_config": {"enable_registration": False}})
    def test_map_cas_user_does_not_register_new_user(self) -> None:
        """Ensures new users are not registered if the enabled registration flag is disabled."""

        # stub out the auth handler
        auth_handler = self.hs.get_auth_handler()
        auth_handler.complete_sso_login = AsyncMock()  # type: ignore[method-assign]

        cas_response = CasResponse("test_user", {})
        request = _mock_request()
        self.get_success(
            self.handler._handle_cas_response(request, cas_response, "redirect_uri", "")
        )

        # check that the auth handler was not called as expected
        auth_handler.complete_sso_login.assert_not_called()


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
