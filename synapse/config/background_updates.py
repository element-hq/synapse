#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 Matrix.org Foundation C.I.C.
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
from typing import Any

from synapse.types import JsonDict

from ._base import Config


class BackgroundUpdateConfig(Config):
    section = "background_updates"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        bg_update_config = config.get("background_updates") or {}

        self.update_duration_ms = bg_update_config.get(
            "background_update_duration_ms", 100
        )

        self.sleep_enabled = bg_update_config.get("sleep_enabled", True)

        self.sleep_duration_ms = bg_update_config.get("sleep_duration_ms", 1000)

        self.min_batch_size = bg_update_config.get("min_batch_size", 1)

        self.default_batch_size = bg_update_config.get("default_batch_size", 100)
