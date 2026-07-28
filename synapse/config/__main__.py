#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright 2015, 2016 OpenMarket Ltd
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
import sys

from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig


def main(args: list[str]) -> None:
    action = args[1] if len(args) > 1 and args[1] == "read" else None
    # If we're reading a key in the config file, then `args[1]` will be `read`  and `args[2]`
    # will be the key to read.
    # We'll want to rework this code if we want to support more actions than just `read`.
    load_config_args = args[3:] if action else args[1:]

    try:
        config = HomeServerConfig.load_config("", load_config_args)
    except ConfigError as e:
        sys.stderr.write("\n" + str(e) + "\n")
        sys.exit(1)

    print("Config parses OK!")

    if action == "read":
        key = args[2]
        key_parts = key.split(".")

        value = config
        try:
            while len(key_parts):
                value = getattr(value, key_parts[0])
                key_parts.pop(0)

            print(f"\n{key}: {value}")
        except AttributeError:
            print(
                f"\nNo '{key}' key could be found in the provided configuration file."
            )
            sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
