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
#

from typing import Any, Optional

from synapse._pydantic_compat import (
    AnyHttpUrl,
    Field,
    FilePath,
    StrictBool,
    StrictStr,
    ValidationError,
    validator,
)
from synapse.config.experimental import read_secret_from_file_once
from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel

from ._base import Config, ConfigError


class MasConfigModel(ParseModel):
    enabled: StrictBool = False
    endpoint: AnyHttpUrl = Field(default="http://localhost:8080")
    secret: Optional[StrictStr] = Field(default=None)
    secret_path: Optional[FilePath] = Field(default=None)

    @validator("secret")
    def validate_secret_is_set_if_enabled(cls, v: Any, values: dict) -> Any:
        if values.get("enabled", False) and not values.get("secret_path") and not v:
            raise ValueError(
                "You must set a `secret` or `secret_path` when enabling Matrix Authentication Service integration."
            )

        return v

    @validator("secret_path")
    def validate_secret_path_is_set_if_enabled(cls, v: Any, values: dict) -> Any:
        if values.get("secret"):
            raise ValueError(
                "`secret` and `secret_path` cannot be set at the same time."
            )

        return v


class MasConfig(Config):
    section = "mas"

    def read_config(
        self, config: JsonDict, allow_secrets_in_config: bool, **kwargs: Any
    ) -> None:
        mas_config = config.get("matrix_authentication_service", {})
        if mas_config is None:
            mas_config = {}

        try:
            parsed = MasConfigModel(**mas_config)
        except ValidationError as e:
            raise ConfigError(
                "Could not validate Matrix Authentication Service configuration",
                path=("matrix_authentication_service",),
            ) from e

        if parsed.secret and not allow_secrets_in_config:
            raise ConfigError(
                "Config options that expect an in-line secret as value are disabled",
                ("matrix_authentication_service", "secret"),
            )

        self.enabled = parsed.enabled
        self.endpoint = parsed.endpoint
        self._secret = parsed.secret
        self._secret_path = parsed.secret_path

    def secret(self) -> str:
        if self._secret is not None:
            return self._secret
        elif self._secret_path is not None:
            return read_secret_from_file_once(
                str(self._secret_path),
                ("matrix_authentication_service", "secret_path"),
            )
        else:
            raise RuntimeError(
                "Neither `secret` nor `secret_path` are set, this is a bug.",
            )
