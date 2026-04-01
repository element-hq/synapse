#! /usr/bin/env python
import sys

if sys.version_info < (3, 11):
    raise RuntimeError("Requires at least Python 3.11, to import tomllib")

import tomllib

with open("uv.lock", "rb") as f:
    lockfile = tomllib.load(f)

try:
    lock_version = lockfile["version"]
    assert lock_version == 1
except Exception:
    print(
        """\
    Lockfile is not version 1. You probably need to upgrade uv on your local box
    and re-run `uv lock`. See the dependency management documentation at
    https://element-hq.github.io/synapse/develop/development/dependencies.html
    """
    )
    raise
