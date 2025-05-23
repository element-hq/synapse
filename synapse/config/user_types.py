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

from typing import Any, List, Optional

from synapse.api.constants import UserTypes
from synapse.types import JsonDict

from ._base import Config


class UserTypesConfig(Config):
    section = "user_types"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        user_types: JsonDict = config.get("user_types", {})

        self.default_user_type: Optional[str] = user_types.get(
            "default_user_type", None
        )
        self.extra_user_types: List[str] = user_types.get("extra_user_types", [])
        # XXXTODO: provide a way to set which extra user types should be treated as "real" users

        all_user_types: List[str] = []
        all_user_types.extend(UserTypes.ALL_USER_TYPES)
        all_user_types.extend(self.extra_user_types)

        self.all_user_types = all_user_types
