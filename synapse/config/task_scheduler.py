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
from typing import Any

from synapse.types import JsonDict

from ._base import Config, ConfigError

DEFAULT_MAX_CONCURRENT_TASKS = 2


class TaskSchedulerConfig(Config):
    section = "task_scheduler"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        task_scheduler_config = config.get("task_scheduler") or {}
        if not isinstance(task_scheduler_config, dict):
            raise ConfigError("'task_scheduler' must be a mapping", ("task_scheduler",))

        max_concurrent_tasks = task_scheduler_config.get(
            "max_concurrent_tasks", DEFAULT_MAX_CONCURRENT_TASKS
        )
        # In Python, bool is a subclass of int (isinstance(True, int) is True).
        # We explicitly check for bool to reject YAML booleans like `true`/`false`.
        if (
            not isinstance(max_concurrent_tasks, int)
            or isinstance(max_concurrent_tasks, bool)
            or max_concurrent_tasks <= 0
        ):
            raise ConfigError(
                "'max_concurrent_tasks' must be a positive integer",
                ("task_scheduler", "max_concurrent_tasks"),
            )

        self.max_concurrent_tasks = max_concurrent_tasks
