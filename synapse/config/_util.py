#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Any, Dict, Type, TypeVar

import jsonschema

from synapse._pydantic_compat import HAS_PYDANTIC_V2

if TYPE_CHECKING or HAS_PYDANTIC_V2:
    from pydantic.v1 import BaseModel, ValidationError, parse_obj_as
else:
    from pydantic import BaseModel, ValidationError, parse_obj_as

from synapse.config._base import ConfigError
from synapse.types import JsonDict, StrSequence


def validate_config(
    json_schema: JsonDict, config: Any, config_path: StrSequence
) -> None:
    """Validates a config setting against a JsonSchema definition

    This can be used to validate a section of the config file against a schema
    definition. If the validation fails, a ConfigError is raised with a textual
    description of the problem.

    Args:
        json_schema: the schema to validate against
        config: the configuration value to be validated
        config_path: the path within the config file. This will be used as a basis
           for the error message.

    Raises:
        ConfigError, if validation fails.
    """
    try:
        jsonschema.validate(config, json_schema)
    except jsonschema.ValidationError as e:
        raise json_error_to_config_error(e, config_path)


def json_error_to_config_error(
    e: jsonschema.ValidationError, config_path: StrSequence
) -> ConfigError:
    """Converts a json validation error to a user-readable ConfigError

    Args:
        e: the exception to be converted
        config_path: the path within the config file. This will be used as a basis
           for the error message.

    Returns:
        a ConfigError
    """
    # copy `config_path` before modifying it.
    path = list(config_path)
    for p in list(e.absolute_path):
        if isinstance(p, int):
            path.append("<item %i>" % p)
        else:
            path.append(str(p))
    return ConfigError(e.message, path)


Model = TypeVar("Model", bound=BaseModel)


def parse_and_validate_mapping(
    config: Any,
    model_type: Type[Model],
) -> Dict[str, Model]:
    """Parse `config` as a mapping from strings to a given `Model` type.
    Args:
        config: The configuration data to check
        model_type: The BaseModel to validate and parse against.
    Returns:
        Fully validated and parsed Dict[str, Model].
    Raises:
        ConfigError, if given improper input.
    """
    try:
        # type-ignore: mypy doesn't like constructing `Dict[str, model_type]` because
        # `model_type` is a runtime variable. Pydantic is fine with this.
        instances = parse_obj_as(Dict[str, model_type], config)  # type: ignore[valid-type]
    except ValidationError as e:
        raise ConfigError(str(e)) from e
    return instances
