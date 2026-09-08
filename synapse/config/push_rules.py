#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from typing import Annotated, Any

from pydantic import (
    Field,
    StrictInt,
    ValidationError,
)

from synapse.config._util import ConfigByteSize
from synapse.types import JsonDict
from synapse.util.pydantic_models import ParseModel

from ._base import Config, ConfigError


class PushRulesLimitsConfig(ParseModel):
    # Chosen arbitrarily, but with the rough rationale that a user
    # might have on the order of 10k rooms and want to set a push rule override for each one.
    rule_count: Annotated[StrictInt, Field(ge=0)] = 10_000

    # Chosen arbitrarily, but with the rationale that room IDs are allowed to be up to 255 bytes
    # and they are often used in rule IDs.
    rule_id_length: Annotated[StrictInt, Field(ge=1)] = 300

    # Chosen arbitrarily, but with the rationale that real-world push rules don't get
    # nearly this big in practice.
    # Even 512 bytes would probably have been fine, but we should leave space for the use cases
    # of push rules to grow in the future.
    rule_size: Annotated[ConfigByteSize, Field(ge=1)] = 1024


class PushRulesConfigModel(ParseModel):
    limits: PushRulesLimitsConfig = Field(default_factory=PushRulesLimitsConfig)


class PushRulesConfig(Config):
    section = "push_rules"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        raw_config = config.get("push_rules", {})

        try:
            parsed = PushRulesConfigModel(**raw_config)
        except ValidationError as e:
            raise ConfigError(
                f"Could not validate configuration: {e}",
                path=("push_rules",),
            ) from e

        self.limits = parsed.limits
