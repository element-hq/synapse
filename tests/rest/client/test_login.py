#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
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
import time
import urllib.parse
from typing import (
    Any,
    BinaryIO,
    Callable,
    Collection,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)
from unittest.mock import Mock
from urllib.parse import urlencode

import pymacaroons
from typing_extensions import Literal

from twisted.test.proto_helpers import MemoryReactor
from twisted.web.resource import Resource

import synapse.rest.admin
from synapse.api.constants import ApprovalNoticeMedium, LoginType
from synapse.api.errors import Codes
from synapse.appservice import ApplicationService
from synapse.http.client import RawHeaders
from synapse.module_api import ModuleApi
from synapse.rest.client import account, devices, login, logout, profile, register
from synapse.rest.client.account import WhoamiRestServlet
from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util import Clock

from tests import unittest
from tests.handlers.test_oidc import HAS_OIDC
from tests.handlers.test_saml import has_saml2
from tests.rest.client.utils import TEST_OIDC_CONFIG
from tests.server import FakeChannel
from tests.test_utils.html_parsers import TestHtmlParser
from tests.test_utils.oidc import FakeOidcServer
from tests.unittest import HomeserverTestCase, override_config, skip_unless

try:
    from authlib.jose import JsonWebKey, jwt

    HAS_JWT = True
except ImportError:
    HAS_JWT = False


# synapse server name: used to populate public_baseurl in some tests
SYNAPSE_SERVER_PUBLIC_HOSTNAME = "synapse"

# public_baseurl for some tests. It uses an http:// scheme because
# FakeChannel.isSecure() returns False, so synapse will see the requested uri as
# http://..., so using http in the public_baseurl stops Synapse trying to redirect to
# https://....
BASE_URL = "http://%s/" % (SYNAPSE_SERVER_PUBLIC_HOSTNAME,)

# CAS server used in some tests
CAS_SERVER = "https://fake.test"

# just enough to tell pysaml2 where to redirect to
SAML_SERVER = "https://test.saml.server/idp/sso"
TEST_SAML_METADATA = """
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata">
  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
      <md:SingleSignOnService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect" Location="%(SAML_SERVER)s"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
""" % {
    "SAML_SERVER": SAML_SERVER,
}

LOGIN_URL = b"/_matrix/client/r0/login"
TEST_URL = b"/_matrix/client/r0/account/whoami"

# a (valid) url with some annoying characters in.  %3D is =, %26 is &, %2B is +
TEST_CLIENT_REDIRECT_URL = 'https://x?<ab c>&q"+%3D%2B"="fö%26=o"'

# the query params in TEST_CLIENT_REDIRECT_URL
EXPECTED_CLIENT_REDIRECT_URL_PARAMS = [("<ab c>", ""), ('q" =+"', '"fö&=o"')]

# Login flows we expect to appear in the list after the normal ones.
ADDITIONAL_LOGIN_FLOWS = [
    {"type": "m.login.application_service"},
]


class TestSpamChecker:
    def __init__(self, config: None, api: ModuleApi):
        api.register_spam_checker_callbacks(
            check_login_for_spam=self.check_login_for_spam,
        )

    @staticmethod
    def parse_config(config: JsonDict) -> None:
        return None

    async def check_login_for_spam(
        self,
        user_id: str,
        device_id: Optional[str],
        initial_display_name: Optional[str],
        request_info: Collection[Tuple[Optional[str], str]],
        auth_provider_id: Optional[str] = None,
    ) -> Union[
        Literal["NOT_SPAM"],
        Tuple["synapse.module_api.errors.Codes", JsonDict],
    ]:
        return "NOT_SPAM"


class DenyAllSpamChecker:
    def __init__(self, config: None, api: ModuleApi):
        api.register_spam_checker_callbacks(
            check_login_for_spam=self.check_login_for_spam,
        )

    @staticmethod
    def parse_config(config: JsonDict) -> None:
        return None

    async def check_login_for_spam(
        self,
        user_id: str,
        device_id: Optional[str],
        initial_display_name: Optional[str],
        request_info: Collection[Tuple[Optional[str], str]],
        auth_provider_id: Optional[str] = None,
    ) -> Union[
        Literal["NOT_SPAM"],
        Tuple["synapse.module_api.errors.Codes", JsonDict],
    ]:
        # Return an odd set of values to ensure that they get correctly passed
        # to the client.
        return Codes.LIMIT_EXCEEDED, {"extra": "value"}


class LoginRestServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        logout.register_servlets,
        devices.register_servlets,
        lambda hs, http_server: WhoamiRestServlet(hs).register(http_server),
        register.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()
        self.hs.config.registration.enable_registration = True
        self.hs.config.registration.registrations_require_3pid = []
        self.hs.config.registration.auto_join_rooms = []
        self.hs.config.captcha.enable_registration_captcha = False

        return self.hs

    @override_config(
        {
            "rc_login": {
                "address": {"per_second": 0.17, "burst_count": 5},
                # Prevent the account login ratelimiter from raising first
                #
                # This is normally covered by the default test homeserver config
                # which sets these values to 10000, but as we're overriding the entire
                # rc_login dict here, we need to set this manually as well
                "account": {"per_second": 10000, "burst_count": 10000},
            },
        }
    )
    def test_POST_ratelimiting_per_address(self) -> None:
        # Create different users so we're sure not to be bothered by the per-user
        # ratelimiter.
        for i in range(6):
            self.register_user("kermit" + str(i), "monkey")

        for i in range(6):
            params = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "kermit" + str(i)},
                "password": "monkey",
            }
            channel = self.make_request(b"POST", LOGIN_URL, params)

            if i == 5:
                self.assertEqual(channel.code, 429, msg=channel.result)
                retry_after_ms = int(channel.json_body["retry_after_ms"])
                retry_header = channel.headers.getRawHeaders("Retry-After")
            else:
                self.assertEqual(channel.code, 200, msg=channel.result)

        # Since we're ratelimiting at 1 request/min, retry_after_ms should be lower
        # than 1min.
        self.assertLess(retry_after_ms, 6000)
        assert retry_header
        self.assertLessEqual(int(retry_header[0]), 6)

        self.reactor.advance(retry_after_ms / 1000.0 + 1.0)

        params = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": "kermit" + str(i)},
            "password": "monkey",
        }
        channel = self.make_request(b"POST", LOGIN_URL, params)

        self.assertEqual(channel.code, 200, msg=channel.result)

    @override_config(
        {
            "rc_login": {
                "account": {"per_second": 0.17, "burst_count": 5},
                # Prevent the address login ratelimiter from raising first
                #
                # This is normally covered by the default test homeserver config
                # which sets these values to 10000, but as we're overriding the entire
                # rc_login dict here, we need to set this manually as well
                "address": {"per_second": 10000, "burst_count": 10000},
            },
        }
    )
    def test_POST_ratelimiting_per_account(self) -> None:
        self.register_user("kermit", "monkey")

        for i in range(6):
            params = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "kermit"},
                "password": "monkey",
            }
            channel = self.make_request(b"POST", LOGIN_URL, params)

            if i == 5:
                self.assertEqual(channel.code, 429, msg=channel.result)
                retry_after_ms = int(channel.json_body["retry_after_ms"])
                retry_header = channel.headers.getRawHeaders("Retry-After")
            else:
                self.assertEqual(channel.code, 200, msg=channel.result)

        # Since we're ratelimiting at 1 request/min, retry_after_ms should be lower
        # than 1min.
        self.assertLess(retry_after_ms, 6000)
        assert retry_header
        self.assertLessEqual(int(retry_header[0]), 6)

        self.reactor.advance(retry_after_ms / 1000.0)

        params = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": "kermit"},
            "password": "monkey",
        }
        channel = self.make_request(b"POST", LOGIN_URL, params)

        self.assertEqual(channel.code, 200, msg=channel.result)

    @override_config(
        {
            "rc_login": {
                # Prevent the address login ratelimiter from raising first
                #
                # This is normally covered by the default test homeserver config
                # which sets these values to 10000, but as we're overriding the entire
                # rc_login dict here, we need to set this manually as well
                "address": {"per_second": 10000, "burst_count": 10000},
                "failed_attempts": {"per_second": 0.17, "burst_count": 5},
            },
        }
    )
    def test_POST_ratelimiting_per_account_failed_attempts(self) -> None:
        self.register_user("kermit", "monkey")

        for i in range(6):
            params = {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "kermit"},
                "password": "notamonkey",
            }
            channel = self.make_request(b"POST", LOGIN_URL, params)

            if i == 5:
                self.assertEqual(channel.code, 429, msg=channel.result)
                retry_after_ms = int(channel.json_body["retry_after_ms"])
                retry_header = channel.headers.getRawHeaders("Retry-After")
            else:
                self.assertEqual(channel.code, 403, msg=channel.result)

        # Since we're ratelimiting at 1 request/min, retry_after_ms should be lower
        # than 1min.
        self.assertLess(retry_after_ms, 6000)
        assert retry_header
        self.assertLessEqual(int(retry_header[0]), 6)

        self.reactor.advance(retry_after_ms / 1000.0 + 1.0)

        params = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": "kermit"},
            "password": "notamonkey",
        }
        channel = self.make_request(b"POST", LOGIN_URL, params)

        self.assertEqual(channel.code, 403, msg=channel.result)

    @override_config({"session_lifetime": "24h"})
    def test_soft_logout(self) -> None:
        self.register_user("kermit", "monkey")

        # we shouldn't be able to make requests without an access token
        channel = self.make_request(b"GET", TEST_URL)
        self.assertEqual(channel.code, 401, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_MISSING_TOKEN")

        # log in as normal
        params = {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": "kermit"},
            "password": "monkey",
        }
        channel = self.make_request(b"POST", LOGIN_URL, params)

        self.assertEqual(channel.code, 200, channel.result)
        access_token = channel.json_body["access_token"]
        device_id = channel.json_body["device_id"]

        # we should now be able to make requests with the access token
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 200, channel.result)

        # time passes
        self.reactor.advance(24 * 3600)

        # ... and we should be soft-logouted
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 401, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")
        self.assertEqual(channel.json_body["soft_logout"], True)

        #
        # test behaviour after deleting the expired device
        #

        # we now log in as a different device
        access_token_2 = self.login("kermit", "monkey")

        # more requests with the expired token should still return a soft-logout
        self.reactor.advance(3600)
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 401, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")
        self.assertEqual(channel.json_body["soft_logout"], True)

        # ... but if we delete that device, it will be a proper logout
        self._delete_device(access_token_2, "kermit", "monkey", device_id)

        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 401, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")
        self.assertEqual(channel.json_body["soft_logout"], False)

    def _delete_device(
        self, access_token: str, user_id: str, password: str, device_id: str
    ) -> None:
        """Perform the UI-Auth to delete a device"""
        channel = self.make_request(
            b"DELETE", "devices/" + device_id, access_token=access_token
        )
        self.assertEqual(channel.code, 401, channel.result)
        # check it's a UI-Auth fail
        self.assertEqual(
            set(channel.json_body.keys()),
            {"flows", "params", "session"},
            channel.result,
        )

        auth = {
            "type": "m.login.password",
            # https://github.com/matrix-org/synapse/issues/5665
            # "identifier": {"type": "m.id.user", "user": user_id},
            "user": user_id,
            "password": password,
            "session": channel.json_body["session"],
        }

        channel = self.make_request(
            b"DELETE",
            "devices/" + device_id,
            access_token=access_token,
            content={"auth": auth},
        )
        self.assertEqual(channel.code, 200, channel.result)

    @override_config({"session_lifetime": "24h"})
    def test_session_can_hard_logout_after_being_soft_logged_out(self) -> None:
        self.register_user("kermit", "monkey")

        # log in as normal
        access_token = self.login("kermit", "monkey")

        # we should now be able to make requests with the access token
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 200, channel.result)

        # time passes
        self.reactor.advance(24 * 3600)

        # ... and we should be soft-logouted
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 401, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")
        self.assertEqual(channel.json_body["soft_logout"], True)

        # Now try to hard logout this session
        channel = self.make_request(b"POST", "/logout", access_token=access_token)
        self.assertEqual(channel.code, 200, msg=channel.result)

    @override_config({"session_lifetime": "24h"})
    def test_session_can_hard_logout_all_sessions_after_being_soft_logged_out(
        self,
    ) -> None:
        self.register_user("kermit", "monkey")

        # log in as normal
        access_token = self.login("kermit", "monkey")

        # we should now be able to make requests with the access token
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 200, channel.result)

        # time passes
        self.reactor.advance(24 * 3600)

        # ... and we should be soft-logouted
        channel = self.make_request(b"GET", TEST_URL, access_token=access_token)
        self.assertEqual(channel.code, 401, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_UNKNOWN_TOKEN")
        self.assertEqual(channel.json_body["soft_logout"], True)

        # Now try to hard log out all of the user's sessions
        channel = self.make_request(b"POST", "/logout/all", access_token=access_token)
        self.assertEqual(channel.code, 200, msg=channel.result)

    def test_login_with_overly_long_device_id_fails(self) -> None:
        self.register_user("mickey", "cheese")

        # create a device_id longer than 512 characters
        device_id = "yolo" * 512

        body = {
            "type": "m.login.password",
            "user": "mickey",
            "password": "cheese",
            "device_id": device_id,
        }

        # make a login request with the bad device_id
        channel = self.make_request(
            "POST",
            "/_matrix/client/v3/login",
            body,
            custom_headers=None,
        )

        # test that the login fails with the correct error code
        self.assertEqual(channel.code, 400)
        self.assertEqual(channel.json_body["errcode"], "M_INVALID_PARAM")

    @override_config(
        {
            "experimental_features": {
                "msc3866": {
                    "enabled": True,
                    "require_approval_for_new_accounts": True,
                }
            }
        }
    )
    def test_require_approval(self) -> None:
        channel = self.make_request(
            "POST",
            "register",
            {
                "username": "kermit",
                "password": "monkey",
                "auth": {"type": LoginType.DUMMY},
            },
        )
        self.assertEqual(403, channel.code, channel.result)
        self.assertEqual(Codes.USER_AWAITING_APPROVAL, channel.json_body["errcode"])
        self.assertEqual(
            ApprovalNoticeMedium.NONE, channel.json_body["approval_notice_medium"]
        )

        params = {
            "type": LoginType.PASSWORD,
            "identifier": {"type": "m.id.user", "user": "kermit"},
            "password": "monkey",
        }
        channel = self.make_request("POST", LOGIN_URL, params)
        self.assertEqual(403, channel.code, channel.result)
        self.assertEqual(Codes.USER_AWAITING_APPROVAL, channel.json_body["errcode"])
        self.assertEqual(
            ApprovalNoticeMedium.NONE, channel.json_body["approval_notice_medium"]
        )

    def test_get_login_flows_with_login_via_existing_disabled(self) -> None:
        """GET /login should return m.login.token without get_login_token"""
        channel = self.make_request("GET", "/_matrix/client/r0/login")
        self.assertEqual(channel.code, 200, channel.result)

        flows = {flow["type"]: flow for flow in channel.json_body["flows"]}
        self.assertNotIn("m.login.token", flows)

    @override_config({"login_via_existing_session": {"enabled": True}})
    def test_get_login_flows_with_login_via_existing_enabled(self) -> None:
        """GET /login should return m.login.token with get_login_token true"""
        channel = self.make_request("GET", "/_matrix/client/r0/login")
        self.assertEqual(channel.code, 200, channel.result)

        self.assertCountEqual(
            channel.json_body["flows"],
            [
                {"type": "m.login.token", "get_login_token": True},
                {"type": "m.login.password"},
                {"type": "m.login.application_service"},
            ],
        )

    @override_config(
        {
            "modules": [
                {
                    "module": TestSpamChecker.__module__
                    + "."
                    + TestSpamChecker.__qualname__
                }
            ]
        }
    )
    def test_spam_checker_allow(self) -> None:
        """Check that that adding a spam checker doesn't break login."""
        self.register_user("kermit", "monkey")

        body = {"type": "m.login.password", "user": "kermit", "password": "monkey"}

        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/login",
            body,
        )
        self.assertEqual(channel.code, 200, channel.result)

    @override_config(
        {
            "modules": [
                {
                    "module": DenyAllSpamChecker.__module__
                    + "."
                    + DenyAllSpamChecker.__qualname__
                }
            ]
        }
    )
    def test_spam_checker_deny(self) -> None:
        """Check that login"""

        self.register_user("kermit", "monkey")

        body = {"type": "m.login.password", "user": "kermit", "password": "monkey"}

        channel = self.make_request(
            "POST",
            "/_matrix/client/r0/login",
            body,
        )
        self.assertEqual(channel.code, 403, channel.result)
        self.assertLessEqual(
            {"errcode": Codes.LIMIT_EXCEEDED, "extra": "value"}.items(),
            channel.json_body.items(),
        )


@skip_unless(has_saml2 and HAS_OIDC, "Requires SAML2 and OIDC")
class MultiSSOTestCase(unittest.HomeserverTestCase):
    """Tests for homeservers with multiple SSO providers enabled"""

    servlets = [
        login.register_servlets,
    ]

    def default_config(self) -> Dict[str, Any]:
        config = super().default_config()

        config["public_baseurl"] = BASE_URL

        config["cas_config"] = {
            "enabled": True,
            "server_url": CAS_SERVER,
            "service_url": "https://matrix.goodserver.com:8448",
        }

        config["saml2_config"] = {
            "sp_config": {
                "metadata": {"inline": [TEST_SAML_METADATA]},
                # use the XMLSecurity backend to avoid relying on xmlsec1
                "crypto_backend": "XMLSecurity",
            },
        }

        # default OIDC provider
        config["oidc_config"] = TEST_OIDC_CONFIG

        # additional OIDC providers
        config["oidc_providers"] = [
            {
                "idp_id": "idp1",
                "idp_name": "IDP1",
                "discover": False,
                "issuer": "https://issuer1",
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "scopes": ["profile"],
                "authorization_endpoint": "https://issuer1/auth",
                "token_endpoint": "https://issuer1/token",
                "userinfo_endpoint": "https://issuer1/userinfo",
                "user_mapping_provider": {
                    "config": {"localpart_template": "{{ user.sub }}"}
                },
            }
        ]
        return config

    def create_resource_dict(self) -> Dict[str, Resource]:
        d = super().create_resource_dict()
        d.update(build_synapse_client_resource_tree(self.hs))
        return d

    def test_get_login_flows(self) -> None:
        """GET /login should return password and SSO flows"""
        channel = self.make_request("GET", "/_matrix/client/r0/login")
        self.assertEqual(channel.code, 200, channel.result)

        expected_flow_types = [
            "m.login.cas",
            "m.login.sso",
            "m.login.token",
            "m.login.password",
        ] + [f["type"] for f in ADDITIONAL_LOGIN_FLOWS]

        self.assertCountEqual(
            [f["type"] for f in channel.json_body["flows"]], expected_flow_types
        )

        flows = {flow["type"]: flow for flow in channel.json_body["flows"]}
        self.assertCountEqual(
            flows["m.login.sso"]["identity_providers"],
            [
                {"id": "cas", "name": "CAS"},
                {"id": "saml", "name": "SAML"},
                {"id": "oidc-idp1", "name": "IDP1"},
                {"id": "oidc", "name": "OIDC"},
            ],
        )

    def test_multi_sso_redirect(self) -> None:
        """/login/sso/redirect should redirect to an identity picker"""
        # first hit the redirect url, which should redirect to our idp picker
        channel = self._make_sso_redirect_request(None)
        self.assertEqual(channel.code, 302, channel.result)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        uri = location_headers[0]

        # hitting that picker should give us some HTML
        channel = self.make_request("GET", uri)
        self.assertEqual(channel.code, 200, channel.result)

        # parse the form to check it has fields assumed elsewhere in this class
        html = channel.result["body"].decode("utf-8")
        p = TestHtmlParser()
        p.feed(html)
        p.close()

        # there should be a link for each href
        returned_idps: List[str] = []
        for link in p.links:
            path, query = link.split("?", 1)
            self.assertEqual(path, "pick_idp")
            params = urllib.parse.parse_qs(query)
            self.assertEqual(params["redirectUrl"], [TEST_CLIENT_REDIRECT_URL])
            returned_idps.append(params["idp"][0])

        self.assertCountEqual(returned_idps, ["cas", "oidc", "oidc-idp1", "saml"])

    def test_multi_sso_redirect_to_cas(self) -> None:
        """If CAS is chosen, should redirect to the CAS server"""

        channel = self.make_request(
            "GET",
            "/_synapse/client/pick_idp?redirectUrl="
            + urllib.parse.quote_plus(TEST_CLIENT_REDIRECT_URL)
            + "&idp=cas",
            shorthand=False,
        )
        self.assertEqual(channel.code, 302, channel.result)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        cas_uri = location_headers[0]
        cas_uri_path, cas_uri_query = cas_uri.split("?", 1)

        # it should redirect us to the login page of the cas server
        self.assertEqual(cas_uri_path, CAS_SERVER + "/login")

        # check that the redirectUrl is correctly encoded in the service param - ie, the
        # place that CAS will redirect to
        cas_uri_params = urllib.parse.parse_qs(cas_uri_query)
        service_uri = cas_uri_params["service"][0]
        _, service_uri_query = service_uri.split("?", 1)
        service_uri_params = urllib.parse.parse_qs(service_uri_query)
        self.assertEqual(service_uri_params["redirectUrl"][0], TEST_CLIENT_REDIRECT_URL)

    def test_multi_sso_redirect_to_saml(self) -> None:
        """If SAML is chosen, should redirect to the SAML server"""
        channel = self.make_request(
            "GET",
            "/_synapse/client/pick_idp?redirectUrl="
            + urllib.parse.quote_plus(TEST_CLIENT_REDIRECT_URL)
            + "&idp=saml",
        )
        self.assertEqual(channel.code, 302, channel.result)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        saml_uri = location_headers[0]
        saml_uri_path, saml_uri_query = saml_uri.split("?", 1)

        # it should redirect us to the login page of the SAML server
        self.assertEqual(saml_uri_path, SAML_SERVER)

        # the RelayState is used to carry the client redirect url
        saml_uri_params = urllib.parse.parse_qs(saml_uri_query)
        relay_state_param = saml_uri_params["RelayState"][0]
        self.assertEqual(relay_state_param, TEST_CLIENT_REDIRECT_URL)

    def test_login_via_oidc(self) -> None:
        """If OIDC is chosen, should redirect to the OIDC auth endpoint"""

        fake_oidc_server = self.helper.fake_oidc_server()

        with fake_oidc_server.patch_homeserver(hs=self.hs):
            # pick the default OIDC provider
            channel = self.make_request(
                "GET",
                "/_synapse/client/pick_idp?redirectUrl="
                + urllib.parse.quote_plus(TEST_CLIENT_REDIRECT_URL)
                + "&idp=oidc",
            )
        self.assertEqual(channel.code, 302, channel.result)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        oidc_uri = location_headers[0]
        oidc_uri_path, oidc_uri_query = oidc_uri.split("?", 1)

        # it should redirect us to the auth page of the OIDC server
        self.assertEqual(oidc_uri_path, fake_oidc_server.authorization_endpoint)

        # ... and should have set a cookie including the redirect url
        cookie_headers = channel.headers.getRawHeaders("Set-Cookie")
        assert cookie_headers
        cookies: Dict[str, str] = {}
        for h in cookie_headers:
            key, value = h.split(";")[0].split("=", maxsplit=1)
            cookies[key] = value

        oidc_session_cookie = cookies["oidc_session"]
        macaroon = pymacaroons.Macaroon.deserialize(oidc_session_cookie)
        self.assertEqual(
            self._get_value_from_macaroon(macaroon, "client_redirect_url"),
            TEST_CLIENT_REDIRECT_URL,
        )

        channel, _ = self.helper.complete_oidc_auth(
            fake_oidc_server, oidc_uri, cookies, {"sub": "user1"}
        )

        # that should serve a confirmation page
        self.assertEqual(channel.code, 200, channel.result)
        content_type_headers = channel.headers.getRawHeaders("Content-Type")
        assert content_type_headers
        self.assertTrue(content_type_headers[-1].startswith("text/html"))
        p = TestHtmlParser()
        p.feed(channel.text_body)
        p.close()

        # ... which should contain our redirect link
        self.assertEqual(len(p.links), 1)
        path, query = p.links[0].split("?", 1)
        self.assertEqual(path, "https://x")

        # it will have url-encoded the params properly, so we'll have to parse them
        params = urllib.parse.parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, errors="strict"
        )
        self.assertEqual(params[0:2], EXPECTED_CLIENT_REDIRECT_URL_PARAMS)
        self.assertEqual(params[2][0], "loginToken")

        # finally, submit the matrix login token to the login API, which gives us our
        # matrix access token, mxid, and device id.
        login_token = params[2][1]
        chan = self.make_request(
            "POST",
            "/login",
            content={"type": "m.login.token", "token": login_token},
        )
        self.assertEqual(chan.code, 200, chan.result)
        self.assertEqual(chan.json_body["user_id"], "@user1:test")

    def test_multi_sso_redirect_to_unknown(self) -> None:
        """An unknown IdP should cause a 400"""
        channel = self.make_request(
            "GET",
            "/_synapse/client/pick_idp?redirectUrl=http://x&idp=xyz",
        )
        self.assertEqual(channel.code, 400, channel.result)

    def test_client_idp_redirect_to_unknown(self) -> None:
        """If the client tries to pick an unknown IdP, return a 404"""
        channel = self._make_sso_redirect_request("xxx")
        self.assertEqual(channel.code, 404, channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_NOT_FOUND")

    def test_client_idp_redirect_to_oidc(self) -> None:
        """If the client pick a known IdP, redirect to it"""
        fake_oidc_server = self.helper.fake_oidc_server()

        with fake_oidc_server.patch_homeserver(hs=self.hs):
            channel = self._make_sso_redirect_request("oidc")
        self.assertEqual(channel.code, 302, channel.result)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        oidc_uri = location_headers[0]
        oidc_uri_path, oidc_uri_query = oidc_uri.split("?", 1)

        # it should redirect us to the auth page of the OIDC server
        self.assertEqual(oidc_uri_path, fake_oidc_server.authorization_endpoint)

    def _make_sso_redirect_request(self, idp_prov: Optional[str] = None) -> FakeChannel:
        """Send a request to /_matrix/client/r0/login/sso/redirect

        ... possibly specifying an IDP provider
        """
        endpoint = "/_matrix/client/r0/login/sso/redirect"
        if idp_prov is not None:
            endpoint += "/" + idp_prov
        endpoint += "?redirectUrl=" + urllib.parse.quote_plus(TEST_CLIENT_REDIRECT_URL)

        return self.make_request(
            "GET",
            endpoint,
            custom_headers=[("Host", SYNAPSE_SERVER_PUBLIC_HOSTNAME)],
        )

    @staticmethod
    def _get_value_from_macaroon(macaroon: pymacaroons.Macaroon, key: str) -> str:
        prefix = key + " = "
        for caveat in macaroon.caveats:
            if caveat.caveat_id.startswith(prefix):
                return caveat.caveat_id[len(prefix) :]
        raise ValueError("No %s caveat in macaroon" % (key,))


class CASTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.base_url = "https://matrix.goodserver.com/"
        self.redirect_path = "_synapse/client/login/sso/redirect/confirm"

        config = self.default_config()
        config["public_baseurl"] = (
            config.get("public_baseurl") or "https://matrix.goodserver.com:8448"
        )
        config["cas_config"] = {
            "enabled": True,
            "server_url": CAS_SERVER,
        }

        cas_user_id = "username"
        self.user_id = "@%s:test" % cas_user_id

        async def get_raw(uri: str, args: Any) -> bytes:
            """Return an example response payload from a call to the `/proxyValidate`
            endpoint of a CAS server, copied from
            https://apereo.github.io/cas/5.0.x/protocol/CAS-Protocol-V2-Specification.html#26-proxyvalidate-cas-20

            This needs to be returned by an async function (as opposed to set as the
            mock's return value) because the corresponding Synapse code awaits on it.
            """
            return (
                """
                <cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>
                  <cas:authenticationSuccess>
                      <cas:user>%s</cas:user>
                      <cas:proxyGrantingTicket>PGTIOU-84678-8a9d...</cas:proxyGrantingTicket>
                      <cas:proxies>
                          <cas:proxy>https://proxy2/pgtUrl</cas:proxy>
                          <cas:proxy>https://proxy1/pgtUrl</cas:proxy>
                      </cas:proxies>
                  </cas:authenticationSuccess>
                </cas:serviceResponse>
            """
                % cas_user_id
            ).encode("utf-8")

        mocked_http_client = Mock(spec=["get_raw"])
        mocked_http_client.get_raw.side_effect = get_raw

        self.hs = self.setup_test_homeserver(
            config=config,
            proxied_http_client=mocked_http_client,
        )

        return self.hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.deactivate_account_handler = hs.get_deactivate_account_handler()

    def test_cas_redirect_confirm(self) -> None:
        """Tests that the SSO login flow serves a confirmation page before redirecting a
        user to the redirect URL.
        """
        base_url = "/_matrix/client/r0/login/cas/ticket?redirectUrl"
        redirect_url = "https://dodgy-site.com/"

        url_parts = list(urllib.parse.urlparse(base_url))
        query = dict(urllib.parse.parse_qsl(url_parts[4]))
        query.update({"redirectUrl": redirect_url})
        query.update({"ticket": "ticket"})
        url_parts[4] = urllib.parse.urlencode(query)
        cas_ticket_url = urllib.parse.urlunparse(url_parts)

        # Get Synapse to call the fake CAS and serve the template.
        channel = self.make_request("GET", cas_ticket_url)

        # Test that the response is HTML.
        self.assertEqual(channel.code, 200, channel.result)
        content_type_header_value = ""
        for header in channel.headers.getRawHeaders("Content-Type", []):
            content_type_header_value = header

        self.assertTrue(content_type_header_value.startswith("text/html"))

        # Test that the body isn't empty.
        self.assertTrue(len(channel.result["body"]) > 0)

        # And that it contains our redirect link
        self.assertIn(redirect_url, channel.result["body"].decode("UTF-8"))

    @override_config(
        {
            "sso": {
                "client_whitelist": [
                    "https://legit-site.com/",
                    "https://other-site.com/",
                ]
            }
        }
    )
    def test_cas_redirect_whitelisted(self) -> None:
        """Tests that the SSO login flow serves a redirect to a whitelisted url"""
        self._test_redirect("https://legit-site.com/")

    @override_config({"public_baseurl": "https://example.com"})
    def test_cas_redirect_login_fallback(self) -> None:
        self._test_redirect("https://example.com/_matrix/static/client/login")

    def _test_redirect(self, redirect_url: str) -> None:
        """Tests that the SSO login flow serves a redirect for the given redirect URL."""
        cas_ticket_url = (
            "/_matrix/client/r0/login/cas/ticket?redirectUrl=%s&ticket=ticket"
            % (urllib.parse.quote(redirect_url))
        )

        # Get Synapse to call the fake CAS and serve the template.
        channel = self.make_request("GET", cas_ticket_url)

        self.assertEqual(channel.code, 302)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        self.assertEqual(location_headers[0][: len(redirect_url)], redirect_url)

    @override_config({"sso": {"client_whitelist": ["https://legit-site.com/"]}})
    def test_deactivated_user(self) -> None:
        """Logging in as a deactivated account should error."""
        redirect_url = "https://legit-site.com/"

        # First login (to create the user).
        self._test_redirect(redirect_url)

        # Deactivate the account.
        self.get_success(
            self.deactivate_account_handler.deactivate_account(
                self.user_id, False, create_requester(self.user_id)
            )
        )

        # Request the CAS ticket.
        cas_ticket_url = (
            "/_matrix/client/r0/login/cas/ticket?redirectUrl=%s&ticket=ticket"
            % (urllib.parse.quote(redirect_url))
        )

        # Get Synapse to call the fake CAS and serve the template.
        channel = self.make_request("GET", cas_ticket_url)

        # Because the user is deactivated they are served an error template.
        self.assertEqual(channel.code, 403)
        self.assertIn(b"SSO account deactivated", channel.result["body"])


@skip_unless(HAS_JWT, "requires authlib")
class JWTTestCase(unittest.HomeserverTestCase):
    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
    ]

    jwt_secret = "secret"
    jwt_algorithm = "HS256"
    base_config = {
        "enabled": True,
        "secret": jwt_secret,
        "algorithm": jwt_algorithm,
    }

    def default_config(self) -> Dict[str, Any]:
        config = super().default_config()

        # If jwt_config has been defined (eg via @override_config), don't replace it.
        if config.get("jwt_config") is None:
            config["jwt_config"] = self.base_config

        return config

    def jwt_encode(self, payload: Dict[str, Any], secret: str = jwt_secret) -> str:
        header = {"alg": self.jwt_algorithm}
        result: bytes = jwt.encode(header, payload, secret)
        return result.decode("ascii")

    def jwt_login(self, *args: Any) -> FakeChannel:
        params = {"type": "org.matrix.login.jwt", "token": self.jwt_encode(*args)}
        channel = self.make_request(b"POST", LOGIN_URL, params)
        return channel

    def test_login_jwt_valid_registered(self) -> None:
        self.register_user("kermit", "monkey")
        channel = self.jwt_login({"sub": "kermit"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@kermit:test")

    def test_login_jwt_valid_unregistered(self) -> None:
        channel = self.jwt_login({"sub": "frog"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@frog:test")

    def test_login_jwt_invalid_signature(self) -> None:
        channel = self.jwt_login({"sub": "frog"}, "notsecret")
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            "JWT validation failed: Signature verification failed",
        )

    def test_login_jwt_expired(self) -> None:
        channel = self.jwt_login({"sub": "frog", "exp": 864000})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            "JWT validation failed: expired_token: The token is expired",
        )

    def test_login_jwt_not_before(self) -> None:
        now = int(time.time())
        channel = self.jwt_login({"sub": "frog", "nbf": now + 3600})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            "JWT validation failed: invalid_token: The token is not valid yet",
        )

    def test_login_no_sub(self) -> None:
        channel = self.jwt_login({"username": "root"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(channel.json_body["error"], "Invalid JWT")

    @override_config({"jwt_config": {**base_config, "issuer": "test-issuer"}})
    def test_login_iss(self) -> None:
        """Test validating the issuer claim."""
        # A valid issuer.
        channel = self.jwt_login({"sub": "kermit", "iss": "test-issuer"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@kermit:test")

        # An invalid issuer.
        channel = self.jwt_login({"sub": "kermit", "iss": "invalid"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            'JWT validation failed: invalid_claim: Invalid claim "iss"',
        )

        # Not providing an issuer.
        channel = self.jwt_login({"sub": "kermit"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            'JWT validation failed: missing_claim: Missing "iss" claim',
        )

    def test_login_iss_no_config(self) -> None:
        """Test providing an issuer claim without requiring it in the configuration."""
        channel = self.jwt_login({"sub": "kermit", "iss": "invalid"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@kermit:test")

    @override_config({"jwt_config": {**base_config, "audiences": ["test-audience"]}})
    def test_login_aud(self) -> None:
        """Test validating the audience claim."""
        # A valid audience.
        channel = self.jwt_login({"sub": "kermit", "aud": "test-audience"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@kermit:test")

        # An invalid audience.
        channel = self.jwt_login({"sub": "kermit", "aud": "invalid"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            'JWT validation failed: invalid_claim: Invalid claim "aud"',
        )

        # Not providing an audience.
        channel = self.jwt_login({"sub": "kermit"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            'JWT validation failed: missing_claim: Missing "aud" claim',
        )

    def test_login_aud_no_config(self) -> None:
        """Test providing an audience without requiring it in the configuration."""
        channel = self.jwt_login({"sub": "kermit", "aud": "invalid"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            'JWT validation failed: invalid_claim: Invalid claim "aud"',
        )

    def test_login_default_sub(self) -> None:
        """Test reading user ID from the default subject claim."""
        channel = self.jwt_login({"sub": "kermit"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@kermit:test")

    @override_config({"jwt_config": {**base_config, "subject_claim": "username"}})
    def test_login_custom_sub(self) -> None:
        """Test reading user ID from a custom subject claim."""
        channel = self.jwt_login({"username": "frog"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@frog:test")

    def test_login_no_token(self) -> None:
        params = {"type": "org.matrix.login.jwt"}
        channel = self.make_request(b"POST", LOGIN_URL, params)
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(channel.json_body["error"], "Token field for JWT is missing")

    def test_deactivated_user(self) -> None:
        """Logging in as a deactivated account should error."""
        user_id = self.register_user("kermit", "monkey")
        self.get_success(
            self.hs.get_deactivate_account_handler().deactivate_account(
                user_id, erase_data=False, requester=create_requester(user_id)
            )
        )

        channel = self.jwt_login({"sub": "kermit"})
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_USER_DEACTIVATED")
        self.assertEqual(
            channel.json_body["error"], "This account has been deactivated"
        )


# The JWTPubKeyTestCase is a complement to JWTTestCase where we instead use
# RSS256, with a public key configured in synapse as "jwt_secret", and tokens
# signed by the private key.
@skip_unless(HAS_JWT, "requires authlib")
class JWTPubKeyTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
    ]

    # This key's pubkey is used as the jwt_secret setting of synapse. Valid
    # tokens are signed by this and validated using the pubkey. It is generated
    # with `openssl genrsa 512` (not a secure way to generate real keys, but
    # good enough for tests!)
    jwt_privatekey = "\n".join(
        [
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIBPAIBAAJBAM50f1Q5gsdmzifLstzLHb5NhfajiOt7TKO1vSEWdq7u9x8SMFiB",
            "492RM9W/XFoh8WUfL9uL6Now6tPRDsWv3xsCAwEAAQJAUv7OOSOtiU+wzJq82rnk",
            "yR4NHqt7XX8BvkZPM7/+EjBRanmZNSp5kYZzKVaZ/gTOM9+9MwlmhidrUOweKfB/",
            "kQIhAPZwHazbjo7dYlJs7wPQz1vd+aHSEH+3uQKIysebkmm3AiEA1nc6mDdmgiUq",
            "TpIN8A4MBKmfZMWTLq6z05y/qjKyxb0CIQDYJxCwTEenIaEa4PdoJl+qmXFasVDN",
            "ZU0+XtNV7yul0wIhAMI9IhiStIjS2EppBa6RSlk+t1oxh2gUWlIh+YVQfZGRAiEA",
            "tqBR7qLZGJ5CVKxWmNhJZGt1QHoUtOch8t9C4IdOZ2g=",
            "-----END RSA PRIVATE KEY-----",
        ]
    )

    # Generated with `openssl rsa -in foo.key -pubout`, with the the above
    # private key placed in foo.key (jwt_privatekey).
    jwt_pubkey = "\n".join(
        [
            "-----BEGIN PUBLIC KEY-----",
            "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBAM50f1Q5gsdmzifLstzLHb5NhfajiOt7",
            "TKO1vSEWdq7u9x8SMFiB492RM9W/XFoh8WUfL9uL6Now6tPRDsWv3xsCAwEAAQ==",
            "-----END PUBLIC KEY-----",
        ]
    )

    # This key is used to sign tokens that shouldn't be accepted by synapse.
    # Generated just like jwt_privatekey.
    bad_privatekey = "\n".join(
        [
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIBOgIBAAJBAL//SQrKpKbjCCnv/FlasJCv+t3k/MPsZfniJe4DVFhsktF2lwQv",
            "gLjmQD3jBUTz+/FndLSBvr3F4OHtGL9O/osCAwEAAQJAJqH0jZJW7Smzo9ShP02L",
            "R6HRZcLExZuUrWI+5ZSP7TaZ1uwJzGFspDrunqaVoPobndw/8VsP8HFyKtceC7vY",
            "uQIhAPdYInDDSJ8rFKGiy3Ajv5KWISBicjevWHF9dbotmNO9AiEAxrdRJVU+EI9I",
            "eB4qRZpY6n4pnwyP0p8f/A3NBaQPG+cCIFlj08aW/PbxNdqYoBdeBA0xDrXKfmbb",
            "iwYxBkwL0JCtAiBYmsi94sJn09u2Y4zpuCbJeDPKzWkbuwQh+W1fhIWQJQIhAKR0",
            "KydN6cRLvphNQ9c/vBTdlzWxzcSxREpguC7F1J1m",
            "-----END RSA PRIVATE KEY-----",
        ]
    )

    def default_config(self) -> Dict[str, Any]:
        config = super().default_config()
        config["jwt_config"] = {
            "enabled": True,
            "secret": self.jwt_pubkey,
            "algorithm": "RS256",
        }
        return config

    def jwt_encode(self, payload: Dict[str, Any], secret: str = jwt_privatekey) -> str:
        header = {"alg": "RS256"}
        if secret.startswith("-----BEGIN RSA PRIVATE KEY-----"):
            secret = JsonWebKey.import_key(secret, {"kty": "RSA"})
        result: bytes = jwt.encode(header, payload, secret)
        return result.decode("ascii")

    def jwt_login(self, *args: Any) -> FakeChannel:
        params = {"type": "org.matrix.login.jwt", "token": self.jwt_encode(*args)}
        channel = self.make_request(b"POST", LOGIN_URL, params)
        return channel

    def test_login_jwt_valid(self) -> None:
        channel = self.jwt_login({"sub": "kermit"})
        self.assertEqual(channel.code, 200, msg=channel.result)
        self.assertEqual(channel.json_body["user_id"], "@kermit:test")

    def test_login_jwt_invalid_signature(self) -> None:
        channel = self.jwt_login({"sub": "frog"}, self.bad_privatekey)
        self.assertEqual(channel.code, 403, msg=channel.result)
        self.assertEqual(channel.json_body["errcode"], "M_FORBIDDEN")
        self.assertEqual(
            channel.json_body["error"],
            "JWT validation failed: Signature verification failed",
        )


AS_USER = "as_user_alice"


class AppserviceLoginRestServletTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        register.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.hs = self.setup_test_homeserver()

        self.service = ApplicationService(
            id="unique_identifier",
            token="some_token",
            sender="@asbot:example.com",
            namespaces={
                ApplicationService.NS_USERS: [
                    {"regex": r"@as_user.*", "exclusive": False}
                ],
                ApplicationService.NS_ROOMS: [],
                ApplicationService.NS_ALIASES: [],
            },
        )
        self.another_service = ApplicationService(
            id="another__identifier",
            token="another_token",
            sender="@as2bot:example.com",
            namespaces={
                ApplicationService.NS_USERS: [
                    {"regex": r"@as2_user.*", "exclusive": False}
                ],
                ApplicationService.NS_ROOMS: [],
                ApplicationService.NS_ALIASES: [],
            },
        )

        self.hs.get_datastores().main.services_cache.append(self.service)
        self.hs.get_datastores().main.services_cache.append(self.another_service)
        return self.hs

    def test_login_appservice_user(self) -> None:
        """Test that an appservice user can use /login"""
        self.register_appservice_user(AS_USER, self.service.token)

        params = {
            "type": login.LoginRestServlet.APPSERVICE_TYPE,
            "identifier": {"type": "m.id.user", "user": AS_USER},
        }
        channel = self.make_request(
            b"POST", LOGIN_URL, params, access_token=self.service.token
        )

        self.assertEqual(channel.code, 200, msg=channel.result)

    def test_login_appservice_user_bot(self) -> None:
        """Test that the appservice bot can use /login"""
        self.register_appservice_user(AS_USER, self.service.token)

        params = {
            "type": login.LoginRestServlet.APPSERVICE_TYPE,
            "identifier": {"type": "m.id.user", "user": self.service.sender},
        }
        channel = self.make_request(
            b"POST", LOGIN_URL, params, access_token=self.service.token
        )

        self.assertEqual(channel.code, 200, msg=channel.result)

    def test_login_appservice_wrong_user(self) -> None:
        """Test that non-as users cannot login with the as token"""
        self.register_appservice_user(AS_USER, self.service.token)

        params = {
            "type": login.LoginRestServlet.APPSERVICE_TYPE,
            "identifier": {"type": "m.id.user", "user": "fibble_wibble"},
        }
        channel = self.make_request(
            b"POST", LOGIN_URL, params, access_token=self.service.token
        )

        self.assertEqual(channel.code, 403, msg=channel.result)

    def test_login_appservice_wrong_as(self) -> None:
        """Test that as users cannot login with wrong as token"""
        self.register_appservice_user(AS_USER, self.service.token)

        params = {
            "type": login.LoginRestServlet.APPSERVICE_TYPE,
            "identifier": {"type": "m.id.user", "user": AS_USER},
        }
        channel = self.make_request(
            b"POST", LOGIN_URL, params, access_token=self.another_service.token
        )

        self.assertEqual(channel.code, 403, msg=channel.result)

    def test_login_appservice_no_token(self) -> None:
        """Test that users must provide a token when using the appservice
        login method
        """
        self.register_appservice_user(AS_USER, self.service.token)

        params = {
            "type": login.LoginRestServlet.APPSERVICE_TYPE,
            "identifier": {"type": "m.id.user", "user": AS_USER},
        }
        channel = self.make_request(b"POST", LOGIN_URL, params)

        self.assertEqual(channel.code, 401, msg=channel.result)


@skip_unless(HAS_OIDC, "requires OIDC")
class UsernamePickerTestCase(HomeserverTestCase):
    """Tests for the username picker flow of SSO login"""

    servlets = [
        login.register_servlets,
        profile.register_servlets,
        account.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.http_client = Mock(spec=["get_file"])
        self.http_client.get_file.side_effect = mock_get_file
        hs = self.setup_test_homeserver(
            proxied_blocklisted_http_client=self.http_client
        )
        return hs

    def default_config(self) -> Dict[str, Any]:
        config = super().default_config()
        config["public_baseurl"] = BASE_URL

        config["oidc_config"] = {}
        config["oidc_config"].update(TEST_OIDC_CONFIG)
        config["oidc_config"]["user_mapping_provider"] = {
            "config": {
                "display_name_template": "{{ user.displayname }}",
                "email_template": "{{ user.email }}",
                "picture_template": "{{ user.picture }}",
            }
        }

        # whitelist this client URI so we redirect straight to it rather than
        # serving a confirmation page
        config["sso"] = {"client_whitelist": ["https://x"]}
        return config

    def create_resource_dict(self) -> Dict[str, Resource]:
        d = super().create_resource_dict()
        d.update(build_synapse_client_resource_tree(self.hs))
        return d

    def proceed_to_username_picker_page(
        self,
        fake_oidc_server: FakeOidcServer,
        displayname: str,
        email: str,
        picture: str,
    ) -> Tuple[str, str]:
        # do the start of the login flow
        channel, _ = self.helper.auth_via_oidc(
            fake_oidc_server,
            {
                "sub": "tester",
                "displayname": displayname,
                "picture": picture,
                "email": email,
            },
            TEST_CLIENT_REDIRECT_URL,
        )

        # that should redirect to the username picker
        self.assertEqual(channel.code, 302, channel.result)
        location_headers = channel.headers.getRawHeaders("Location")
        assert location_headers
        picker_url = location_headers[0]
        self.assertEqual(picker_url, "/_synapse/client/pick_username/account_details")

        # ... with a username_mapping_session cookie
        cookies: Dict[str, str] = {}
        channel.extract_cookies(cookies)
        self.assertIn("username_mapping_session", cookies)
        session_id = cookies["username_mapping_session"]

        # introspect the sso handler a bit to check that the username mapping session
        # looks ok.
        username_mapping_sessions = self.hs.get_sso_handler()._username_mapping_sessions
        self.assertIn(
            session_id,
            username_mapping_sessions,
            "session id not found in map",
        )
        session = username_mapping_sessions[session_id]
        self.assertEqual(session.remote_user_id, "tester")
        self.assertEqual(session.display_name, displayname)
        self.assertEqual(session.emails, [email])
        self.assertEqual(session.avatar_url, picture)
        self.assertEqual(session.client_redirect_url, TEST_CLIENT_REDIRECT_URL)

        # the expiry time should be about 15 minutes away
        expected_expiry = self.clock.time_msec() + (15 * 60 * 1000)
        self.assertApproximates(session.expiry_time_ms, expected_expiry, tolerance=1000)

        return picker_url, session_id

    def test_username_picker_use_displayname_avatar_and_email(self) -> None:
        """Test the happy path of a username picker flow with using displayname, avatar and email."""

        fake_oidc_server = self.helper.fake_oidc_server()

        mxid = "@bobby:test"
        displayname = "Jonny"
        email = "bobby@test.com"
        picture = "mxc://test/avatar_url"

        picker_url, session_id = self.proceed_to_username_picker_page(
            fake_oidc_server, displayname, email, picture
        )

        # Now, submit a username to the username picker, which should serve a redirect
        # to the completion page.
        # Also specify that we should use the provided displayname, avatar and email.
        content = urlencode(
            {
                b"username": b"bobby",
                b"use_display_name": b"true",
                b"use_avatar": b"true",
                b"use_email": email,
            }
        ).encode("utf8")
        chan = self.make_request(
            "POST",
            path=picker_url,
            content=content,
            content_is_form=True,
            custom_headers=[
                ("Cookie", "username_mapping_session=" + session_id),
                # old versions of twisted don't do form-parsing without a valid
                # content-length header.
                ("Content-Length", str(len(content))),
            ],
        )
        self.assertEqual(chan.code, 302, chan.result)
        location_headers = chan.headers.getRawHeaders("Location")
        assert location_headers

        # send a request to the completion page, which should 302 to the client redirectUrl
        chan = self.make_request(
            "GET",
            path=location_headers[0],
            custom_headers=[("Cookie", "username_mapping_session=" + session_id)],
        )
        self.assertEqual(chan.code, 302, chan.result)
        location_headers = chan.headers.getRawHeaders("Location")
        assert location_headers

        # ensure that the returned location matches the requested redirect URL
        path, query = location_headers[0].split("?", 1)
        self.assertEqual(path, "https://x")

        # it will have url-encoded the params properly, so we'll have to parse them
        params = urllib.parse.parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, errors="strict"
        )
        self.assertEqual(params[0:2], EXPECTED_CLIENT_REDIRECT_URL_PARAMS)
        self.assertEqual(params[2][0], "loginToken")

        # fish the login token out of the returned redirect uri
        login_token = params[2][1]

        # finally, submit the matrix login token to the login API, which gives us our
        # matrix access token, mxid, and device id.
        chan = self.make_request(
            "POST",
            "/login",
            content={"type": "m.login.token", "token": login_token},
        )
        self.assertEqual(chan.code, 200, chan.result)
        self.assertEqual(chan.json_body["user_id"], mxid)

        # ensure the displayname and avatar from the OIDC response have been configured for the user.
        channel = self.make_request(
            "GET", "/profile/" + mxid, access_token=chan.json_body["access_token"]
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertIn("mxc://test", channel.json_body["avatar_url"])
        self.assertEqual(displayname, channel.json_body["displayname"])

        # ensure the email from the OIDC response has been configured for the user.
        channel = self.make_request(
            "GET", "/account/3pid", access_token=chan.json_body["access_token"]
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertEqual(email, channel.json_body["threepids"][0]["address"])

    def test_username_picker_dont_use_displayname_avatar_or_email(self) -> None:
        """Test the happy path of a username picker flow without using displayname, avatar or email."""

        fake_oidc_server = self.helper.fake_oidc_server()

        mxid = "@bobby:test"
        displayname = "Jonny"
        email = "bobby@test.com"
        picture = "mxc://test/avatar_url"
        username = "bobby"

        picker_url, session_id = self.proceed_to_username_picker_page(
            fake_oidc_server, displayname, email, picture
        )

        # Now, submit a username to the username picker, which should serve a redirect
        # to the completion page.
        # Also specify that we should not use the provided displayname, avatar or email.
        content = urlencode(
            {
                b"username": username,
                b"use_display_name": b"false",
                b"use_avatar": b"false",
            }
        ).encode("utf8")
        chan = self.make_request(
            "POST",
            path=picker_url,
            content=content,
            content_is_form=True,
            custom_headers=[
                ("Cookie", "username_mapping_session=" + session_id),
                # old versions of twisted don't do form-parsing without a valid
                # content-length header.
                ("Content-Length", str(len(content))),
            ],
        )
        self.assertEqual(chan.code, 302, chan.result)
        location_headers = chan.headers.getRawHeaders("Location")
        assert location_headers

        # send a request to the completion page, which should 302 to the client redirectUrl
        chan = self.make_request(
            "GET",
            path=location_headers[0],
            custom_headers=[("Cookie", "username_mapping_session=" + session_id)],
        )
        self.assertEqual(chan.code, 302, chan.result)
        location_headers = chan.headers.getRawHeaders("Location")
        assert location_headers

        # ensure that the returned location matches the requested redirect URL
        path, query = location_headers[0].split("?", 1)
        self.assertEqual(path, "https://x")

        # it will have url-encoded the params properly, so we'll have to parse them
        params = urllib.parse.parse_qsl(
            query, keep_blank_values=True, strict_parsing=True, errors="strict"
        )
        self.assertEqual(params[0:2], EXPECTED_CLIENT_REDIRECT_URL_PARAMS)
        self.assertEqual(params[2][0], "loginToken")

        # fish the login token out of the returned redirect uri
        login_token = params[2][1]

        # finally, submit the matrix login token to the login API, which gives us our
        # matrix access token, mxid, and device id.
        chan = self.make_request(
            "POST",
            "/login",
            content={"type": "m.login.token", "token": login_token},
        )
        self.assertEqual(chan.code, 200, chan.result)
        self.assertEqual(chan.json_body["user_id"], mxid)

        # ensure the displayname and avatar from the OIDC response have not been configured for the user.
        channel = self.make_request(
            "GET", "/profile/" + mxid, access_token=chan.json_body["access_token"]
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertNotIn("avatar_url", channel.json_body)
        self.assertEqual(username, channel.json_body["displayname"])

        # ensure the email from the OIDC response has not been configured for the user.
        channel = self.make_request(
            "GET", "/account/3pid", access_token=chan.json_body["access_token"]
        )
        self.assertEqual(channel.code, 200, channel.result)
        self.assertListEqual([], channel.json_body["threepids"])


async def mock_get_file(
    url: str,
    output_stream: BinaryIO,
    max_size: Optional[int] = None,
    headers: Optional[RawHeaders] = None,
    is_allowed_content_type: Optional[Callable[[str], bool]] = None,
) -> Tuple[int, Dict[bytes, List[bytes]], str, int]:
    return 0, {b"Content-Type": [b"image/png"]}, "", 200
