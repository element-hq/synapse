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
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import importlib
import importlib.util
from types import ModuleType
from typing import Any, Tuple, Type

import jsonschema

from synapse.config._base import ConfigError
from synapse.config._util import json_error_to_config_error
from synapse.types import StrSequence


def load_module(provider: dict, config_path: StrSequence) -> Tuple[Type, Any]:
    """Loads a synapse module with its config

    Args:
        provider: a dict with keys 'module' (the module name) and 'config'
           (the config dict).
        config_path: the path within the config file. This will be used as a basis
           for any error message.

    Returns
        Tuple of (provider class, parsed config object)
    """

    modulename = provider.get("module")
    if not isinstance(modulename, str):
        raise ConfigError("expected a string", path=tuple(config_path) + ("module",))

    # We need to import the module, and then pick the class out of
    # that, so we split based on the last dot.
    module_name, clz = modulename.rsplit(".", 1)
    module = importlib.import_module(module_name)
    provider_class = getattr(module, clz)

    # Load the module config. If None, pass an empty dictionary instead
    module_config = provider.get("config") or {}
    if hasattr(provider_class, "parse_config"):
        try:
            provider_config = provider_class.parse_config(module_config)
        except jsonschema.ValidationError as e:
            raise json_error_to_config_error(e, tuple(config_path) + ("config",))
        except ConfigError as e:
            raise _wrap_config_error(
                "Failed to parse config for module %r" % (modulename,),
                prefix=tuple(config_path) + ("config",),
                e=e,
            )
        except Exception as e:
            raise ConfigError(
                "Failed to parse config for module %r" % (modulename,),
                path=tuple(config_path) + ("config",),
            ) from e
    else:
        provider_config = module_config

    return provider_class, provider_config


def load_python_module(location: str) -> ModuleType:
    """Load a python module, and return a reference to its global namespace

    Args:
        location: path to the module

    Returns:
        python module object
    """
    spec = importlib.util.spec_from_file_location(location, location)
    if spec is None:
        raise Exception("Unable to load module at %s" % (location,))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def _wrap_config_error(msg: str, prefix: StrSequence, e: ConfigError) -> "ConfigError":
    """Wrap a relative ConfigError with a new path

    This is useful when we have a ConfigError with a relative path due to a problem
    parsing part of the config, and we now need to set it in context.
    """
    path = prefix
    if e.path:
        path = tuple(prefix) + tuple(e.path)

    e1 = ConfigError(msg, path)

    # ideally we would set the 'cause' of the new exception to the original exception;
    # however now that we have merged the path into our own, the stringification of
    # e will be incorrect, so instead we create a new exception with just the "msg"
    # part.

    e1.__cause__ = Exception(e.msg)
    e1.__cause__.__cause__ = e.__cause__
    return e1
