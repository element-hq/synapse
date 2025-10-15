#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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

import argparse

import synapse.app.homeserver
from synapse.config import ConfigError
from synapse.config.homeserver import HomeServerConfig

from tests.config.utils import ConfigFileTestCase
from tests.utils import default_config


class RegistrationConfigTestCase(ConfigFileTestCase):
    def test_session_lifetime_must_not_be_exceeded_by_smaller_lifetimes(self) -> None:
        """
        session_lifetime should logically be larger than, or at least as large as,
        all the different token lifetimes.
        Test that the user is faced with configuration errors if they make it
        smaller, as that configuration doesn't make sense.
        """
        config_dict = default_config("test")

        # First test all the error conditions
        with self.assertRaises(ConfigError):
            HomeServerConfig().parse_config_dict(
                {
                    "session_lifetime": "30m",
                    "nonrefreshable_access_token_lifetime": "31m",
                    **config_dict,
                },
                "",
                "",
            )

        with self.assertRaises(ConfigError):
            HomeServerConfig().parse_config_dict(
                {
                    "session_lifetime": "30m",
                    "refreshable_access_token_lifetime": "31m",
                    **config_dict,
                },
                "",
                "",
            )

        with self.assertRaises(ConfigError):
            HomeServerConfig().parse_config_dict(
                {
                    "session_lifetime": "30m",
                    "refresh_token_lifetime": "31m",
                    **config_dict,
                },
                "",
                "",
            )

        # Then test all the fine conditions
        HomeServerConfig().parse_config_dict(
            {
                "session_lifetime": "31m",
                "nonrefreshable_access_token_lifetime": "31m",
                **config_dict,
            },
            "",
            "",
        )

        HomeServerConfig().parse_config_dict(
            {
                "session_lifetime": "31m",
                "refreshable_access_token_lifetime": "31m",
                **config_dict,
            },
            "",
            "",
        )

        HomeServerConfig().parse_config_dict(
            {"session_lifetime": "31m", "refresh_token_lifetime": "31m", **config_dict},
            "",
            "",
        )

    def test_refuse_to_start_if_open_registration_and_no_verification(self) -> None:
        """
        Test that our utilities to start the main Synapse homeserver process refuses
        to start if we detect open registration.
        """
        self.generate_config()
        self.add_lines_to_config(
            [
                " ",
                "enable_registration: true",
                "registrations_require_3pid: []",
                "enable_registration_captcha: false",
                "registration_requires_token: false",
            ]
        )

        # Test that allowing open registration without verification raises an error
        with self.assertRaises(SystemExit):
            # Do a normal homeserver creation and setup
            homeserver_config = synapse.app.homeserver.load_or_generate_config(
                ["-c", self.config_file]
            )
            # XXX: The error will be raised at this point
            hs = synapse.app.homeserver.create_homeserver(homeserver_config)
            # Continue with the setup. We don't expect this to run because we raised
            # earlier, but in the future, the code could be refactored to raise the
            # error in a different place.
            synapse.app.homeserver.setup(hs)

    def test_load_config_error_if_open_registration_and_no_verification(self) -> None:
        """
        Test that `HomeServerConfig.load_config(...)` raises an exception when we detect open
        registration.
        """
        self.generate_config()
        self.add_lines_to_config(
            [
                " ",
                "enable_registration: true",
                "registrations_require_3pid: []",
                "enable_registration_captcha: false",
                "registration_requires_token: false",
            ]
        )

        # Test that allowing open registration without verification raises an error
        with self.assertRaises(ConfigError):
            _homeserver_config = HomeServerConfig.load_config(
                description="test", argv_options=["-c", self.config_file]
            )

    def test_load_or_generate_config_error_if_open_registration_and_no_verification(
        self,
    ) -> None:
        """
        Test that `HomeServerConfig.load_or_generate_config(...)` raises an exception when we detect open
        registration.
        """
        self.generate_config()
        self.add_lines_to_config(
            [
                " ",
                "enable_registration: true",
                "registrations_require_3pid: []",
                "enable_registration_captcha: false",
                "registration_requires_token: false",
            ]
        )

        # Test that allowing open registration without verification raises an error
        with self.assertRaises(ConfigError):
            _homeserver_config = HomeServerConfig.load_or_generate_config(
                description="test", argv_options=["-c", self.config_file]
            )

    def test_load_config_with_parser_error_if_open_registration_and_no_verification(
        self,
    ) -> None:
        """
        Test that `HomeServerConfig.load_config_with_parser(...)` raises an exception when we detect open
        registration.
        """
        self.generate_config()
        self.add_lines_to_config(
            [
                " ",
                "enable_registration: true",
                "registrations_require_3pid: []",
                "enable_registration_captcha: false",
                "registration_requires_token: false",
            ]
        )

        # Test that allowing open registration without verification raises an error
        with self.assertRaises(ConfigError):
            config_parser = argparse.ArgumentParser(description="test")
            HomeServerConfig.add_arguments_to_parser(config_parser)

            _homeserver_config = HomeServerConfig.load_config_with_parser(
                parser=config_parser, argv_options=["-c", self.config_file]
            )
