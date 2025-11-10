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

from typing import Any

from pydantic import Field, StrictStr, ValidationError, model_validator
from typing_extensions import Self

from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel

from ._base import Config, ConfigError


class TransportConfigModel(ParseModel):
    type: StrictStr

    livekit_service_url: StrictStr | None = Field(default=None)
    """An optional livekit service URL. Only required if type is "livekit"."""

    @model_validator(mode="after")
    def validate_livekit_service_url(self) -> Self:
        if self.type == "livekit" and not self.livekit_service_url:
            raise ValueError(
                "You must set a `livekit_service_url` when using the 'livekit' transport."
            )
        return self


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
