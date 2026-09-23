#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creation Ltd
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


class SafetyConfig(Config):
    section = "safety"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        safety_config = config.get("safety_policy", {})
        self.policyserv_url = safety_config.get("policyserv_url", None)
        self.policyserv_api_key = safety_config.get("policyserv_api_key", None)
        self.enable_search_redirection = safety_config.get(
            "enable_search_redirection", False
        )
