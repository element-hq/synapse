#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright 2016 OpenMarket Ltd
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
import tempfile
from typing import Callable
from unittest import mock

import yaml
from parameterized import parameterized

from synapse.config import ConfigError
from synapse.config._base import RootConfig
from synapse.config.homeserver import HomeServerConfig

from tests.config.utils import ConfigFileTestCase

try:
    import authlib
except ImportError:
    authlib = None

try:
    import hiredis
except ImportError:
    hiredis = None  # type: ignore


class ConfigLoadingFileTestCase(ConfigFileTestCase):
    def test_load_fails_if_server_name_missing(self) -> None:
        self.generate_config_and_remove_lines_containing(["server_name"])
        with self.assertRaises(ConfigError):
            HomeServerConfig.load_config("", ["-c", self.config_file])
        with self.assertRaises(ConfigError):
            HomeServerConfig.load_or_generate_config("", ["-c", self.config_file])

    def test_generates_and_loads_macaroon_secret_key(self) -> None:
        self.generate_config()

        with open(self.config_file) as f:
            raw = yaml.safe_load(f)
        self.assertIn("macaroon_secret_key", raw)

        config = HomeServerConfig.load_config("", ["-c", self.config_file])
        self.assertTrue(
            hasattr(config.key, "macaroon_secret_key"),
            "Want config to have attr macaroon_secret_key",
        )
        if len(config.key.macaroon_secret_key) < 5:
            self.fail(
                "Want macaroon secret key to be string of at least length 5,"
                "was: %r" % (config.key.macaroon_secret_key,)
            )

        config2 = HomeServerConfig.load_or_generate_config("", ["-c", self.config_file])
        assert config2 is not None
        self.assertTrue(
            hasattr(config2.key, "macaroon_secret_key"),
            "Want config to have attr macaroon_secret_key",
        )
        if len(config2.key.macaroon_secret_key) < 5:
            self.fail(
                "Want macaroon secret key to be string of at least length 5,"
                "was: %r" % (config2.key.macaroon_secret_key,)
            )

    def test_load_succeeds_if_macaroon_secret_key_missing(self) -> None:
        self.generate_config_and_remove_lines_containing(["macaroon"])
        config1 = HomeServerConfig.load_config("", ["-c", self.config_file])
        config2 = HomeServerConfig.load_config("", ["-c", self.config_file])
        config3 = HomeServerConfig.load_or_generate_config("", ["-c", self.config_file])
        assert config1 is not None
        assert config2 is not None
        assert config3 is not None
        self.assertEqual(
            config1.key.macaroon_secret_key, config2.key.macaroon_secret_key
        )
        self.assertEqual(
            config1.key.macaroon_secret_key, config3.key.macaroon_secret_key
        )

    def test_disable_registration(self) -> None:
        self.generate_config()
        self.add_lines_to_config(
            [
                "enable_registration: true",
                "disable_registration: true",
                # We're not worried about open registration in this test. This test is
                # focused on making sure that enable/disable_registration properly
                # override each other.
                "enable_registration_without_verification: true",
            ]
        )
        # Check that disable_registration clobbers enable_registration.
        config = HomeServerConfig.load_config("", ["-c", self.config_file])
        self.assertFalse(config.registration.enable_registration)

        config2 = HomeServerConfig.load_or_generate_config("", ["-c", self.config_file])
        assert config2 is not None
        self.assertFalse(config2.registration.enable_registration)

        # Check that either config value is clobbered by the command line.
        config3 = HomeServerConfig.load_or_generate_config(
            "", ["-c", self.config_file, "--enable-registration"]
        )
        assert config3 is not None
        self.assertTrue(config3.registration.enable_registration)

    def test_stats_enabled(self) -> None:
        self.generate_config_and_remove_lines_containing(["enable_metrics"])
        self.add_lines_to_config(["enable_metrics: true"])

        # The default Metrics Flags are off by default.
        config = HomeServerConfig.load_config("", ["-c", self.config_file])
        self.assertFalse(config.metrics.metrics_flags.known_servers)

    def test_depreciated_identity_server_flag_throws_error(self) -> None:
        self.generate_config()
        # Needed to ensure that actual key/value pair added below don't end up on a line with a comment
        self.add_lines_to_config([" "])
        # Check that presence of "trust_identity_server_for_password" throws config error
        self.add_lines_to_config(["trust_identity_server_for_password_resets: true"])
        with self.assertRaises(ConfigError):
            HomeServerConfig.load_config("", ["-c", self.config_file])

    @parameterized.expand(
        [
            "turn_shared_secret_path: /does/not/exist",
            "registration_shared_secret_path: /does/not/exist",
            "macaroon_secret_key_path: /does/not/exist",
            "recaptcha_private_key_path: /does/not/exist",
            "recaptcha_public_key_path: /does/not/exist",
            "form_secret_path: /does/not/exist",
            "worker_replication_secret_path: /does/not/exist",
            "experimental_features:\n  msc3861:\n    client_secret_path: /does/not/exist",
            "experimental_features:\n  msc3861:\n    admin_token_path: /does/not/exist",
            *["redis:\n  enabled: true\n  password_path: /does/not/exist"]
            * (hiredis is not None),
        ]
    )
    def test_secret_files_missing(self, config_str: str) -> None:
        self.generate_config()
        self.add_lines_to_config(["", config_str])

        with self.assertRaises(ConfigError):
            HomeServerConfig.load_config("", ["-c", self.config_file])

    @parameterized.expand(
        [
            (
                "turn_shared_secret_path: {}",
                lambda c: c.voip.turn_shared_secret.encode("utf-8"),
            ),
            (
                "registration_shared_secret_path: {}",
                lambda c: c.registration.registration_shared_secret.encode("utf-8"),
            ),
            (
                "macaroon_secret_key_path: {}",
                lambda c: c.key.macaroon_secret_key,
            ),
            (
                "recaptcha_private_key_path: {}",
                lambda c: c.captcha.recaptcha_private_key.encode("utf-8"),
            ),
            (
                "recaptcha_public_key_path: {}",
                lambda c: c.captcha.recaptcha_public_key.encode("utf-8"),
            ),
            (
                "form_secret_path: {}",
                lambda c: c.key.form_secret.encode("utf-8"),
            ),
            (
                "worker_replication_secret_path: {}",
                lambda c: c.worker.worker_replication_secret.encode("utf-8"),
            ),
            (
                "experimental_features:\n  msc3861:\n    client_secret_path: {}",
                lambda c: c.experimental.msc3861.client_secret().encode("utf-8"),
            ),
            (
                "experimental_features:\n  msc3861:\n    admin_token_path: {}",
                lambda c: c.experimental.msc3861.admin_token().encode("utf-8"),
            ),
            *[
                (
                    "redis:\n  enabled: true\n  password_path: {}",
                    lambda c: c.redis.redis_password.encode("utf-8"),
                )
            ]
            * (hiredis is not None),
        ]
    )
    def test_secret_files_existing(
        self, config_line: str, get_secret: Callable[[RootConfig], str]
    ) -> None:
        self.generate_config_and_remove_lines_containing(
            ["form_secret", "macaroon_secret_key", "registration_shared_secret"]
        )
        with tempfile.NamedTemporaryFile(buffering=0) as secret_file:
            secret_file.write(b"53C237")

            self.add_lines_to_config(["", config_line.format(secret_file.name)])
            config = HomeServerConfig.load_config("", ["-c", self.config_file])

            self.assertEqual(get_secret(config), b"53C237")

    @parameterized.expand(
        [
            "turn_shared_secret: 53C237",
            "registration_shared_secret: 53C237",
            "macaroon_secret_key: 53C237",
            "recaptcha_private_key: 53C237",
            "recaptcha_public_key: ¬53C237",
            "form_secret: 53C237",
            "worker_replication_secret: 53C237",
            *[
                "experimental_features:\n"
                "  msc3861:\n"
                "    enabled: true\n"
                "    client_secret: 53C237"
            ]
            * (authlib is not None),
            *[
                "experimental_features:\n"
                "  msc3861:\n"
                "    enabled: true\n"
                "    client_auth_method: private_key_jwt\n"
                '    jwk: {{"mock": "mock"}}'
            ]
            * (authlib is not None),
            *[
                "experimental_features:\n"
                "  msc3861:\n"
                "    enabled: true\n"
                "    admin_token: 53C237\n"
                "    client_secret_path: {secret_file}"
            ]
            * (authlib is not None),
            *["redis:\n  enabled: true\n  password: 53C237"] * (hiredis is not None),
        ]
    )
    def test_no_secrets_in_config(self, config_line: str) -> None:
        if authlib is not None:
            patcher = mock.patch("authlib.jose.rfc7517.JsonWebKey.import_key")
            self.addCleanup(patcher.stop)
            patcher.start()

        with tempfile.NamedTemporaryFile(buffering=0) as secret_file:
            # Only used for less mocking with admin_token
            secret_file.write(b"53C237")

            self.generate_config_and_remove_lines_containing(
                ["form_secret", "macaroon_secret_key", "registration_shared_secret"]
            )
            # Check strict mode with no offenders.
            HomeServerConfig.load_config(
                "", ["-c", self.config_file, "--no-secrets-in-config"]
            )
            self.add_lines_to_config(
                ["", config_line.format(secret_file=secret_file.name)]
            )
            # Check strict mode with a single offender.
            with self.assertRaises(ConfigError):
                HomeServerConfig.load_config(
                    "", ["-c", self.config_file, "--no-secrets-in-config"]
                )

            # Check lenient mode with a single offender.
            HomeServerConfig.load_config("", ["-c", self.config_file])

    def test_no_secrets_in_config_but_in_files(self) -> None:
        with tempfile.NamedTemporaryFile(buffering=0) as secret_file:
            secret_file.write(b"53C237")

            self.generate_config_and_remove_lines_containing(
                ["form_secret", "macaroon_secret_key", "registration_shared_secret"]
            )
            self.add_lines_to_config(
                [
                    "",
                    f"turn_shared_secret_path: {secret_file.name}",
                    f"registration_shared_secret_path: {secret_file.name}",
                    f"macaroon_secret_key_path: {secret_file.name}",
                    f"recaptcha_private_key_path: {secret_file.name}",
                    f"recaptcha_public_key_path: {secret_file.name}",
                    f"form_secret_path: {secret_file.name}",
                    f"worker_replication_secret_path: {secret_file.name}",
                    *[
                        "experimental_features:\n"
                        "  msc3861:\n"
                        "    enabled: true\n"
                        f"    admin_token_path: {secret_file.name}\n"
                        f"    client_secret_path: {secret_file.name}\n"
                        # f"    jwk_path: {secret_file.name}"
                    ]
                    * (authlib is not None),
                    *[f"redis:\n  enabled: true\n  password_path: {secret_file.name}"]
                    * (hiredis is not None),
                ]
            )
            HomeServerConfig.load_config(
                "", ["-c", self.config_file, "--no-secrets-in-config"]
            )
