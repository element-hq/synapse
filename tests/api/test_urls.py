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


from twisted.internet.testing import MemoryReactor

from synapse.api.urls import LoginSSORedirectURIBuilder
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase

# a (valid) url with some annoying characters in.  %3D is =, %26 is &, %2B is +
TRICKY_TEST_CLIENT_REDIRECT_URL = 'https://x?<ab c>&q"+%3D%2B"="fö%26=o"'


class LoginSSORedirectURIBuilderTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.login_sso_redirect_url_builder = LoginSSORedirectURIBuilder(hs.config)

    def test_no_idp_id(self) -> None:
        self.assertEqual(
            self.login_sso_redirect_url_builder.build_login_sso_redirect_uri(
                idp_id=None, client_redirect_url="http://example.com/redirect"
            ),
            "https://test/_matrix/client/v3/login/sso/redirect?redirectUrl=http%3A%2F%2Fexample.com%2Fredirect",
        )

    def test_explicit_idp_id(self) -> None:
        self.assertEqual(
            self.login_sso_redirect_url_builder.build_login_sso_redirect_uri(
                idp_id="oidc-github", client_redirect_url="http://example.com/redirect"
            ),
            "https://test/_matrix/client/v3/login/sso/redirect/oidc-github?redirectUrl=http%3A%2F%2Fexample.com%2Fredirect",
        )

    def test_tricky_redirect_uri(self) -> None:
        self.assertEqual(
            self.login_sso_redirect_url_builder.build_login_sso_redirect_uri(
                idp_id="oidc-github",
                client_redirect_url=TRICKY_TEST_CLIENT_REDIRECT_URL,
            ),
            "https://test/_matrix/client/v3/login/sso/redirect/oidc-github?redirectUrl=https%3A%2F%2Fx%3F%3Cab+c%3E%26q%22%2B%253D%252B%22%3D%22f%C3%B6%2526%3Do%22",
        )

    def test_idp_id_with_slash_is_escaped(self) -> None:
        """
        Test to make sure that we properly URL encode the IdP ID.
        """
        self.assertEqual(
            self.login_sso_redirect_url_builder.build_login_sso_redirect_uri(
                idp_id="foo/bar",
                client_redirect_url="http://example.com/redirect",
            ),
            "https://test/_matrix/client/v3/login/sso/redirect/foo%2Fbar?redirectUrl=http%3A%2F%2Fexample.com%2Fredirect",
        )

    def test_url_as_idp_id_is_escaped(self) -> None:
        """
        Test to make sure that we properly URL encode the IdP ID.

        The IdP ID shouldn't be a URL.
        """
        self.assertEqual(
            self.login_sso_redirect_url_builder.build_login_sso_redirect_uri(
                idp_id="http://should-not-be-url.com/",
                client_redirect_url="http://example.com/redirect",
            ),
            "https://test/_matrix/client/v3/login/sso/redirect/http%3A%2F%2Fshould-not-be-url.com%2F?redirectUrl=http%3A%2F%2Fexample.com%2Fredirect",
        )
