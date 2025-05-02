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

from typing import Any

from synapse.types import JsonDict

from ._base import Config


class UserDirectoryConfig(Config):
    """User Directory Configuration
    Configuration for the behaviour of the /user_directory API
    """

    section = "userdirectory"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        user_directory_config = config.get("user_directory") or {}
        self.user_directory_search_enabled = user_directory_config.get("enabled", True)
        self.user_directory_search_all_users = user_directory_config.get(
            "search_all_users", False
        )
        self.user_directory_search_prefer_local_users = user_directory_config.get(
            "prefer_local_users", False
        )
        self.show_locked_users = user_directory_config.get("show_locked_users", False)
