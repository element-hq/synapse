#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from synapse.util.module_loader import load_module

from ._base import Config


class ThirdPartyRulesConfig(Config):
    section = "thirdpartyrules"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.third_party_event_rules = None

        provider = config.get("third_party_event_rules", None)
        if provider is not None:
            self.third_party_event_rules = load_module(
                provider, ("third_party_event_rules",)
            )
