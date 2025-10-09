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
# [This file includes modifications made by New Vector Limited]
#
#

from typing import Any, Optional

from pydantic import ValidationError

from synapse._pydantic_compat import Field, StrictStr, validator
from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel

from ._base import Config, ConfigError


class TransportConfigModel(ParseModel):
    type: StrictStr

    livekit_service_url: Optional[StrictStr] = Field(default=None)
    """An optional livekit service URL. Only required if type is "livekit"."""

    @validator("livekit_service_url", always=True)
    def validate_livekit_service_url(cls, v: Any, values: dict) -> Any:
        if values.get("type") == "livekit" and not v:
            raise ValueError(
                "You must set a `livekit_service_url` when using the 'livekit' transport."
            )

        return v


class MatrixRtcConfigModel(ParseModel):
    transports: list = []


class MatrixRtcConfig(Config):
    section = "matrix_rtc"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        matrix_rtc = config.get("matrix_rtc", {})
        if matrix_rtc is None:
            matrix_rtc = {}

        try:
            parsed = MatrixRtcConfigModel(**matrix_rtc)
        except ValidationError as e:
            raise ConfigError(
                "Could not validate matrix_rtc config",
                ("matrix_rtc",),
            ) from e

        self.transports = parsed.transports
