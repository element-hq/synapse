#!/usr/bin/env python
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

# Wraps `auditwheel repair` to first check if we're repairing a potentially abi3
# compatible wheel, if so rename the wheel before repairing it.

import argparse
import os
import subprocess
from typing import Optional
from zipfile import ZipFile

from packaging.tags import Tag
from packaging.utils import parse_wheel_filename
from packaging.version import Version


def check_is_abi3_compatible(wheel_file: str) -> None:
    """Check the contents of the built wheel for any `.so` files that are *not*
    abi3 compatible.
    """

    with ZipFile(wheel_file, "r") as wheel:
        for file in wheel.namelist():
            if not file.endswith(".so"):
                continue

            if not file.endswith(".abi3.so"):
                raise Exception(f"Found non-abi3 lib: {file}")


def cpython(wheel_file: str, name: str, version: Version, tag: Tag) -> str:
    """Replaces the cpython wheel file with a ABI3 compatible wheel"""

    if tag.abi == "abi3":
        # Nothing to do.
        return wheel_file

    check_is_abi3_compatible(wheel_file)

    # HACK: it seems that some older versions of pip will consider a wheel marked
    # as macosx_11_0 as incompatible with Big Sur. I haven't done the full archaeology
    # here; there are some clues in
    #     https://github.com/pantsbuild/pants/pull/12857
    #     https://github.com/pypa/pip/issues/9138
    #     https://github.com/pypa/packaging/pull/319
    # Empirically this seems to work, note that macOS 11 and 10.16 are the same,
    # both versions are valid for backwards compatibility.
    platform = tag.platform.replace("macosx_11_0", "macosx_10_16")
    abi3_tag = Tag(tag.interpreter, "abi3", platform)

    dirname = os.path.dirname(wheel_file)
    new_wheel_file = os.path.join(
        dirname,
        f"{name}-{version}-{abi3_tag}.whl",
    )

    os.rename(wheel_file, new_wheel_file)

    print("Renamed wheel to", new_wheel_file)

    return new_wheel_file


def main(wheel_file: str, dest_dir: str, archs: Optional[str]) -> None:
    """Entry point"""

    # Parse the wheel file name into its parts. Note that `parse_wheel_filename`
    # normalizes the package name (i.e. it converts matrix_synapse ->
    # matrix-synapse), which is not what we want.
    _, version, build, tags = parse_wheel_filename(os.path.basename(wheel_file))
    name = os.path.basename(wheel_file).split("-")[0]

    if len(tags) != 1:
        # We expect only a wheel file with only a single tag
        raise Exception(f"Unexpectedly found multiple tags: {tags}")

    tag = next(iter(tags))

    if build:
        # We don't use build tags in Synapse
        raise Exception(f"Unexpected build tag: {build}")

    # If the wheel is for cpython then convert it into an abi3 wheel.
    if tag.interpreter.startswith("cp"):
        wheel_file = cpython(wheel_file, name, version, tag)

    # Finally, repair the wheel.
    if archs is not None:
        # If we are given archs then we are on macos and need to use
        # `delocate-listdeps`.
        subprocess.run(["delocate-listdeps", wheel_file], check=True)
        subprocess.run(
            ["delocate-wheel", "--require-archs", archs, "-w", dest_dir, wheel_file],
            check=True,
        )
    else:
        subprocess.run(["auditwheel", "repair", "-w", dest_dir, wheel_file], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tag wheel as abi3 and repair it.")

    parser.add_argument(
        "--wheel-dir",
        "-w",
        metavar="WHEEL_DIR",
        help="Directory to store delocated wheels",
        required=True,
    )

    parser.add_argument(
        "--require-archs",
        metavar="archs",
        default=None,
    )

    parser.add_argument(
        "wheel_file",
        metavar="WHEEL_FILE",
    )

    args = parser.parse_args()

    wheel_file = args.wheel_file
    wheel_dir = args.wheel_dir
    archs = args.require_archs

    main(wheel_file, wheel_dir, archs)
