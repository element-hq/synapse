#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 Matrix.org Foundation C.I.C.
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

import os
import tempfile
from pathlib import Path
from unittest.mock import Mock

from synapse.config import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.module_api import ModuleApi
from synapse.types import JsonDict

from tests.server import get_clock, setup_test_homeserver
from tests.unittest import TestCase, skip_unless
from tests.utils import HAS_AUTHLIB, default_config

# These are a few constants that are used as config parameters in the tests.
SERVER_NAME = "test"
ISSUER = "https://issuer/"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
BASE_URL = "https://synapse/"


class CustomAuthModule:
    """A module which registers a password auth provider."""

    @staticmethod
    def parse_config(config: JsonDict) -> None:
        pass

    def __init__(self, config: None, api: ModuleApi):
        api.register_password_auth_provider_callbacks(
            auth_checkers={("m.login.password", ("password",)): Mock()},
        )


class MasAuthDelegation(TestCase):
    """Test that the Homeserver fails to initialize if the config is invalid."""

    def setUp(self) -> None:
        self.config_dict: JsonDict = {
            **default_config(server_name="test"),
            "public_baseurl": BASE_URL,
            "enable_registration": False,
            "matrix_authentication_service": {
                "enabled": True,
                "endpoint": "http://localhost:1324/",
                "secret": "verysecret",
            },
        }

    def parse_config(self) -> HomeServerConfig:
        config = HomeServerConfig()
        config.parse_config_dict(self.config_dict, "", "")
        return config

    def test_endpoint_has_to_be_a_url(self) -> None:
        self.config_dict["matrix_authentication_service"]["endpoint"] = "not a url"
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_secret_and_secret_path_are_mutually_exclusive(self) -> None:
        with tempfile.NamedTemporaryFile() as f:
            self.config_dict["matrix_authentication_service"]["secret"] = "verysecret"
            self.config_dict["matrix_authentication_service"]["secret_path"] = Path(
                f.name
            )
            with self.assertRaises(ConfigError):
                self.parse_config()

    def test_secret_path_loads_secret(self) -> None:
        with tempfile.NamedTemporaryFile(buffering=0) as f:
            f.write(b"53C237")
            del self.config_dict["matrix_authentication_service"]["secret"]
            self.config_dict["matrix_authentication_service"]["secret_path"] = Path(
                f.name
            )
            config = self.parse_config()
            self.assertEqual(config.mas.secret(), "53C237")

    def test_secret_path_must_exist(self) -> None:
        del self.config_dict["matrix_authentication_service"]["secret"]
        self.config_dict["matrix_authentication_service"]["secret_path"] = Path(
            "/not/a/valid/file"
        )
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_registration_cannot_be_enabled(self) -> None:
        self.config_dict["enable_registration"] = True
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_user_consent_cannot_be_enabled(self) -> None:
        tmpdir = self.mktemp()
        os.mkdir(tmpdir)
        self.config_dict["user_consent"] = {
            "require_at_registration": True,
            "version": "1",
            "template_dir": tmpdir,
            "server_notice_content": {
                "msgtype": "m.text",
                "body": "foo",
            },
        }
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_password_config_cannot_be_enabled(self) -> None:
        self.config_dict["password_config"] = {"enabled": True}
        with self.assertRaises(ConfigError):
            self.parse_config()

    @skip_unless(HAS_AUTHLIB, "requires authlib")
    def test_oidc_sso_cannot_be_enabled(self) -> None:
        self.config_dict["oidc_providers"] = [
            {
                "idp_id": "microsoft",
                "idp_name": "Microsoft",
                "issuer": "https://login.microsoftonline.com/<tenant id>/v2.0",
                "client_id": "<client id>",
                "client_secret": "<client secret>",
                "scopes": ["openid", "profile"],
                "authorization_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/<tenant id>/oauth2/v2.0/token",
                "userinfo_endpoint": "https://graph.microsoft.com/oidc/userinfo",
            }
        ]

        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_cas_sso_cannot_be_enabled(self) -> None:
        self.config_dict["cas_config"] = {
            "enabled": True,
            "server_url": "https://cas-server.com",
            "displayname_attribute": "name",
            "required_attributes": {"userGroup": "staff", "department": "None"},
        }

        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_auth_providers_cannot_be_enabled(self) -> None:
        self.config_dict["modules"] = [
            {
                "module": f"{__name__}.{CustomAuthModule.__qualname__}",
                "config": {},
            }
        ]

        # This requires actually setting up an HS, as the module will be run on setup,
        # which should raise as the module tries to register an auth provider
        config = self.parse_config()
        reactor, clock = get_clock()
        with self.assertRaises(ConfigError):
            setup_test_homeserver(
                cleanup_func=self.addCleanup,
                config=config,
                reactor=reactor,
                clock=clock,
            )

    @skip_unless(HAS_AUTHLIB, "requires authlib")
    def test_jwt_auth_cannot_be_enabled(self) -> None:
        self.config_dict["jwt_config"] = {
            "enabled": True,
            "secret": "my-secret-token",
            "algorithm": "HS256",
        }

        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_login_via_existing_session_cannot_be_enabled(self) -> None:
        self.config_dict["login_via_existing_session"] = {"enabled": True}
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_captcha_cannot_be_enabled(self) -> None:
        self.config_dict.update(
            enable_registration_captcha=True,
            recaptcha_public_key="test",
            recaptcha_private_key="test",
        )
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_refreshable_tokens_cannot_be_enabled(self) -> None:
        self.config_dict.update(
            refresh_token_lifetime="24h",
            refreshable_access_token_lifetime="10m",
            nonrefreshable_access_token_lifetime="24h",
        )
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_session_lifetime_cannot_be_set(self) -> None:
        self.config_dict["session_lifetime"] = "24h"
        with self.assertRaises(ConfigError):
            self.parse_config()

    def test_enable_3pid_changes_cannot_be_enabled(self) -> None:
        self.config_dict["enable_3pid_changes"] = True
        with self.assertRaises(ConfigError):
            self.parse_config()
