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
import yaml

from synapse.config._base import ConfigError
from synapse.config.task_scheduler import TaskSchedulerConfig

from tests.unittest import HomeserverTestCase, override_config


class TaskSchedulerConfigTestCase(HomeserverTestCase):
    def test_default_configuration(self) -> None:
        self.assertEqual(self.hs.config.task_scheduler.max_concurrent_tasks, 2)
        task_scheduler = self.hs.get_task_scheduler()
        self.assertEqual(task_scheduler._max_concurrent_tasks, 2)

    @override_config(
        yaml.safe_load(
            """
            task_scheduler:
                max_concurrent_tasks: 4
            """
        )
    )
    def test_custom_configuration(self) -> None:
        self.assertEqual(self.hs.config.task_scheduler.max_concurrent_tasks, 4)
        task_scheduler = self.hs.get_task_scheduler()
        self.assertEqual(task_scheduler._max_concurrent_tasks, 4)

    def test_invalid_configuration(self) -> None:
        config = TaskSchedulerConfig()
        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": 0}})

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": -1}})

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": "not-an-int"}})

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": {"max_concurrent_tasks": True}})

        with self.assertRaises(ConfigError):
            config.read_config({"task_scheduler": "not-a-dict"})
