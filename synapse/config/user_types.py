#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import Any

from synapse.api.constants import UserTypes
from synapse.types import JsonDict

from ._base import Config, ConfigError


class UserTypesConfig(Config):
    section = "user_types"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        user_types: JsonDict = config.get("user_types", {})

        self.default_user_type: str | None = user_types.get("default_user_type", None)
        self.extra_user_types: list[str] = user_types.get("extra_user_types", [])

        all_user_types: list[str] = []
        all_user_types.extend(UserTypes.ALL_BUILTIN_USER_TYPES)
        all_user_types.extend(self.extra_user_types)

        self.all_user_types = all_user_types

        if self.default_user_type is not None:
            if self.default_user_type not in all_user_types:
                raise ConfigError(
                    f"Default user type {self.default_user_type} is not in the list of all user types: {all_user_types}"
                )
