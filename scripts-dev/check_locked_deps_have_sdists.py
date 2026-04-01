#! /usr/bin/env python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def main() -> None:
    lockfile_path = Path(__file__).parent.parent.joinpath("uv.lock")
    with open(lockfile_path, "rb") as lockfile:
        lockfile_content = tomllib.load(lockfile)

    packages: list[dict] = lockfile_content["package"]

    success = True
    checked = 0

    for package in packages:
        package_name = package["name"]

        # Skip the project itself
        if package_name == "matrix-synapse":
            continue

        checked += 1

        if "sdist" not in package:
            success = False
            print(
                f"Locked package {package_name!r} does not have a source distribution!",
                file=sys.stderr,
            )

    if not success:
        print(
            "\nThere were some problems with the uv lockfile (uv.lock).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"uv lockfile OK. {checked} locked packages checked.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
