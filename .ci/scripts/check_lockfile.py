#! /usr/bin/env python
import tomllib

with open("poetry.lock", "rb") as f:
    lockfile = tomllib.load(f)

try:
    lock_version = lockfile["metadata"]["lock-version"]
    assert lock_version == "2.1"
except Exception:
    print(
        """\
    Lockfile is not version 2.1. You probably need to upgrade poetry on your local box
    and re-run `poetry lock`. See the Poetry cheat sheet at
    https://element-hq.github.io/synapse/develop/development/dependencies.html
    """
    )
    raise
