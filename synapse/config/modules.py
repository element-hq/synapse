#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import Any, Dict, List, Tuple

from synapse.config._base import Config, ConfigError
from synapse.types import JsonDict
from synapse.util.module_loader import load_module


class ModulesConfig(Config):
    section = "modules"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        self.loaded_modules: List[Tuple[Any, Dict]] = []

        configured_modules = config.get("modules") or []
        for i, module in enumerate(configured_modules):
            config_path = ("modules", "<item %i>" % i)
            if not isinstance(module, dict):
                raise ConfigError("expected a mapping", config_path)

            self.loaded_modules.append(load_module(module, config_path))
