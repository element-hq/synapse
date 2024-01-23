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
            synapse.app.homeserver.setup(["-c", self.config_file])
