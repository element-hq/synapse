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

from typing import Any

from synapse.config._base import Config
from synapse.types import JsonDict


class ExtensionsConfig(Config):
    """Config section for enabling extension features"""

    section = "extensions"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.federation_whitelist_endpoint: bool = config.get(
            "extension_federation_whitelist_endpoint", False
        )
