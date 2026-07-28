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

import tomli


def main() -> None:
    lockfile_path = Path(__file__).parent.parent.joinpath("poetry.lock")
    with open(lockfile_path, "rb") as lockfile:
        lockfile_content = tomli.load(lockfile)

    # Poetry 1.3+ lockfile format:
    # There's a `files` inline table in each [[package]]
    packages_to_assets: dict[str, list[dict[str, str]]] = {
        package["name"]: package["files"] for package in lockfile_content["package"]
    }

    success = True

    for package_name, assets in packages_to_assets.items():
        has_sdist = any(asset["file"].endswith(".tar.gz") for asset in assets)
        if not has_sdist:
            success = False
            print(
                f"Locked package {package_name!r} does not have a source distribution!",
                file=sys.stderr,
            )

    if not success:
        print(
            "\nThere were some problems with the Poetry lockfile (poetry.lock).",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"Poetry lockfile OK. {len(packages_to_assets)} locked packages checked.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
