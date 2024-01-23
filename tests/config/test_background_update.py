#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
import yaml

from synapse.storage.background_updates import BackgroundUpdater

from tests.unittest import HomeserverTestCase, override_config


class BackgroundUpdateConfigTestCase(HomeserverTestCase):
    # Tests that the default values in the config are correctly loaded. Note that the default
    # values are loaded when the corresponding config options are commented out, which is why there isn't
    # a config specified here.
    def test_default_configuration(self) -> None:
        background_updater = BackgroundUpdater(
            self.hs, self.hs.get_datastores().main.db_pool
        )

        self.assertEqual(background_updater.minimum_background_batch_size, 1)
        self.assertEqual(background_updater.default_background_batch_size, 100)
        self.assertEqual(background_updater.sleep_enabled, True)
        self.assertEqual(background_updater.sleep_duration_ms, 1000)
        self.assertEqual(background_updater.update_duration_ms, 100)

    # Tests that non-default values for the config options are properly picked up and passed on.
    @override_config(
        yaml.safe_load(
            """
            background_updates:
                background_update_duration_ms: 1000
                sleep_enabled: false
                sleep_duration_ms: 600
                min_batch_size: 5
                default_batch_size: 50
            """
        )
    )
    def test_custom_configuration(self) -> None:
        background_updater = BackgroundUpdater(
            self.hs, self.hs.get_datastores().main.db_pool
        )

        self.assertEqual(background_updater.minimum_background_batch_size, 5)
        self.assertEqual(background_updater.default_background_batch_size, 50)
        self.assertEqual(background_updater.sleep_enabled, False)
        self.assertEqual(background_updater.sleep_duration_ms, 600)
        self.assertEqual(background_updater.update_duration_ms, 1000)
