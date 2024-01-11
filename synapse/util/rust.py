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

import os
import sys
from hashlib import blake2b

import synapse
from synapse.synapse_rust import get_rust_file_digest


def check_rust_lib_up_to_date() -> None:
    """For editable installs check if the rust library is outdated and needs to
    be rebuilt.
    """

    if not _dist_is_editable():
        return

    synapse_dir = os.path.dirname(synapse.__file__)
    synapse_root = os.path.abspath(os.path.join(synapse_dir, ".."))

    # Double check we've not gone into site-packages...
    if os.path.basename(synapse_root) == "site-packages":
        return

    # ... and it looks like the root of a python project.
    if not os.path.exists("pyproject.toml"):
        return

    # Get the hash of all Rust source files
    hash = _hash_rust_files_in_directory(os.path.join(synapse_root, "rust", "src"))

    if hash != get_rust_file_digest():
        raise Exception("Rust module outdated. Please rebuild using `poetry install`")


def _hash_rust_files_in_directory(directory: str) -> str:
    """Get the hash of all files in a directory (recursively)"""

    directory = os.path.abspath(directory)

    paths = []

    dirs = [directory]
    while dirs:
        dir = dirs.pop()
        with os.scandir(dir) as d:
            for entry in d:
                if entry.is_dir():
                    dirs.append(entry.path)
                else:
                    paths.append(entry.path)

    # We sort to make sure that we get a consistent and well-defined ordering.
    paths.sort()

    hasher = blake2b()

    for path in paths:
        with open(os.path.join(directory, path), "rb") as f:
            hasher.update(f.read())

    return hasher.hexdigest()


def _dist_is_editable() -> bool:
    """Is distribution an editable install?"""
    for path_item in sys.path:
        egg_link = os.path.join(path_item, "matrix-synapse.egg-link")
        if os.path.isfile(egg_link):
            return True
    return False
