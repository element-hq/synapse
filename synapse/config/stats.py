#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

import logging
from typing import Any

from synapse.types import JsonDict

from ._base import Config

ROOM_STATS_DISABLED_WARN = """\
WARNING: room/user statistics have been disabled via the stats.enabled
configuration setting. This means that certain features (such as the room
directory) will not operate correctly. Future versions of Synapse may ignore
this setting.

To fix this warning, remove the stats.enabled setting from your configuration
file.
--------------------------------------------------------------------------------"""

logger = logging.getLogger(__name__)


class StatsConfig(Config):
    """Stats Configuration
    Configuration for the behaviour of synapse's stats engine
    """

    section = "stats"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.stats_enabled = True
        stats_config = config.get("stats", None)
        if stats_config:
            self.stats_enabled = stats_config.get("enabled", self.stats_enabled)
        if not self.stats_enabled:
            logger.warning(ROOM_STATS_DISABLED_WARN)
