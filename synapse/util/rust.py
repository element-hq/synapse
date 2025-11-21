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

import json
import os
import urllib.parse
from hashlib import blake2b
from importlib.metadata import Distribution, PackageNotFoundError

import synapse
from synapse.synapse_rust import get_rust_file_digest


def check_rust_lib_up_to_date() -> None:
    """For editable installs check if the rust library is outdated and needs to
    be rebuilt.
    """

    # Get the location of the editable install.
    synapse_root = get_synapse_source_directory()
    if synapse_root is None:
        return None

    # Get the hash of all Rust source files
    rust_path = os.path.join(synapse_root, "rust", "src")
    if not os.path.exists(rust_path):
        return None

    hash = _hash_rust_files_in_directory(rust_path)

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


def get_synapse_source_directory() -> str | None:
    """Try and find the source directory of synapse for editable installs (like
    those used in development).

    Returns None if not an editable install (or otherwise can't find the source
    directory).
    """

    # Try and find the installed matrix-synapse package.
    try:
        package = Distribution.from_name("matrix-synapse")
    except PackageNotFoundError:
        # The package is not found, so it's not installed and so must be being
        # pulled out from a local directory (usually the current one).
        synapse_dir = os.path.dirname(synapse.__file__)
        synapse_root = os.path.abspath(os.path.join(synapse_dir, ".."))

        # Double check we've not gone into site-packages...
        if os.path.basename(synapse_root) == "site-packages":
            return None

        # ... and it looks like the root of a python project.
        if not os.path.exists("pyproject.toml"):
            return None

        return synapse_root

    # Read the `direct_url.json` metadata for the package. This won't exist for
    # packages installed via a repository/etc.
    # c.f. https://packaging.python.org/en/latest/specifications/direct-url/
    direct_url_json = package.read_text("direct_url.json")
    if direct_url_json is None:
        return None

    # c.f. https://packaging.python.org/en/latest/specifications/direct-url/ for
    # the format
    direct_url_dict: dict = json.loads(direct_url_json)

    # `url` must exist as a key, and point to where we fetched the repo from.
    project_url = urllib.parse.urlparse(direct_url_dict["url"])

    # If its not a local file then we must have built the rust libs either a)
    # after we downloaded the package, or b) we built the download wheel.
    if project_url.scheme != "file":
        return None

    # And finally if its not an editable install then the files can't have
    # changed since we installed the package.
    if not direct_url_dict.get("dir_info", {}).get("editable", False):
        return None

    return project_url.path
