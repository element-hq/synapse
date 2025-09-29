#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

from ._base import Config, ConfigError


class MatrixRtcConfig(Config):
    section = "matrix_rtc"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:

        matrix_rtc: JsonDict = config.get("matrix_rtc", {})
        self.transports = matrix_rtc.get("transports", [])

        if not isinstance(self.transports, list):
            raise ConfigError(
                "MatrixRTC transports needs to be an array of transports",
                ("matrix_rtc",)
            )

        if any(("type" not in e for e in self.transports)):
            raise ConfigError(
                "MatrixRTC transport is missing type",
                ("matrix_rtc",)
            )
