#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Nevil Anson Dsouza
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#
from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.config.task_scheduler import TaskSchedulerConfig

from tests.unittest import TestCase
from tests.utils import default_config


class TaskSchedulerConfigTestCase(TestCase):
    def test_default_configuration(self) -> None:
        config_dict = default_config(server_name="test")
        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        self.assertEqual(config.task_scheduler.max_concurrent_tasks, 2)

    def test_custom_configuration(self) -> None:
        config_dict = default_config(server_name="test")
        config_dict["task_scheduler"] = {"max_concurrent_tasks": 4}
        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        self.assertEqual(config.task_scheduler.max_concurrent_tasks, 4)

    def test_invalid_configuration(self) -> None:
        config = TaskSchedulerConfig()
        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": 0}})

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": -1}})

        with self.assertRaises(ConfigError):
            config.read_config(
                {"task_scheduler": {"max_concurrent_tasks": "not-an-int"}}
            )

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": True}})

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": "not-a-dict"})
