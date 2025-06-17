#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
#  Copyright 2022 The Matrix.org Foundation C.I.C.
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

"""
This module exposes a single function which checks synapse's dependencies are present
and correctly versioned. It makes use of `importlib.metadata` to do so. The details
are a bit murky: there's no easy way to get a map from "extras" to the packages they
require. But this is probably just symptomatic of Python's package management.
"""

import logging
from importlib import metadata
from typing import Iterable, NamedTuple, Optional

from packaging.requirements import Requirement

DISTRIBUTION_NAME = "matrix-synapse"


__all__ = ["check_requirements"]

logger = logging.getLogger(__name__)


class DependencyException(Exception):
    @property
    def message(self) -> str:
        return "\n".join(
            [
                "Missing Requirements: %s" % (", ".join(self.dependencies),),
                "To install run:",
                "    pip install --upgrade --force %s" % (" ".join(self.dependencies),),
                "",
            ]
        )

    @property
    def dependencies(self) -> Iterable[str]:
        for i in self.args[0]:
            yield '"' + i + '"'


DEV_EXTRAS = {"lint", "mypy", "test", "dev"}
ALL_EXTRAS = metadata.metadata(DISTRIBUTION_NAME).get_all("Provides-Extra")
assert ALL_EXTRAS is not None
RUNTIME_EXTRAS = set(ALL_EXTRAS) - DEV_EXTRAS
VERSION = metadata.version(DISTRIBUTION_NAME)


def _is_dev_dependency(req: Requirement) -> bool:
    return req.marker is not None and any(
        req.marker.evaluate({"extra": e}) for e in DEV_EXTRAS
    )


def _should_ignore_runtime_requirement(req: Requirement) -> bool:
    # This is a build-time dependency. Irritatingly, `poetry build` ignores the
    # requirements listed in the [build-system] section of pyproject.toml, so in order
    # to support `poetry install --no-dev` we have to mark it as a runtime dependency.
    # See discussion on https://github.com/python-poetry/poetry/issues/6154 (it sounds
    # like the poetry authors don't consider this a bug?)
    #
    # In any case, workaround this by ignoring setuptools_rust here. (It might be
    # slightly cleaner to put `setuptools_rust` in a `build` extra or similar, but for
    # now let's do something quick and dirty.
    if req.name == "setuptools_rust":
        return True
    return False


class Dependency(NamedTuple):
    requirement: Requirement
    must_be_installed: bool


def _generic_dependencies() -> Iterable[Dependency]:
    """Yield pairs (requirement, must_be_installed)."""
    requirements = metadata.requires(DISTRIBUTION_NAME)
    assert requirements is not None
    for raw_requirement in requirements:
        req = Requirement(raw_requirement)
        if _is_dev_dependency(req) or _should_ignore_runtime_requirement(req):
            continue

        # https://packaging.pypa.io/en/latest/markers.html#usage notes that
        #   > Evaluating an extra marker with no environment is an error
        # so we pass in a dummy empty extra value here.
        must_be_installed = req.marker is None or req.marker.evaluate({"extra": ""})
        yield Dependency(req, must_be_installed)


def _dependencies_for_extra(extra: str) -> Iterable[Dependency]:
    """Yield additional dependencies needed for a given `extra`."""
    requirements = metadata.requires(DISTRIBUTION_NAME)
    assert requirements is not None
    for raw_requirement in requirements:
        req = Requirement(raw_requirement)
        if _is_dev_dependency(req):
            continue
        # Exclude mandatory deps by only selecting deps needed with this extra.
        if (
            req.marker is not None
            and req.marker.evaluate({"extra": extra})
            and not req.marker.evaluate({"extra": ""})
        ):
            yield Dependency(req, True)


def _not_installed(requirement: Requirement, extra: Optional[str] = None) -> str:
    if extra:
        return (
            f"Synapse {VERSION} needs {requirement.name} for {extra}, "
            f"but it is not installed"
        )
    else:
        return f"Synapse {VERSION} needs {requirement.name}, but it is not installed"


def _incorrect_version(
    requirement: Requirement, got: str, extra: Optional[str] = None
) -> str:
    if extra:
        return (
            f"Synapse {VERSION} needs {requirement} for {extra}, "
            f"but got {requirement.name}=={got}"
        )
    else:
        return (
            f"Synapse {VERSION} needs {requirement}, but got {requirement.name}=={got}"
        )


def _no_reported_version(requirement: Requirement, extra: Optional[str] = None) -> str:
    if extra:
        return (
            f"Synapse {VERSION} needs {requirement} for {extra}, "
            f"but can't determine {requirement.name}'s version"
        )
    else:
        return (
            f"Synapse {VERSION} needs {requirement}, "
            f"but can't determine {requirement.name}'s version"
        )


def check_requirements(extra: Optional[str] = None) -> None:
    """Check Synapse's dependencies are present and correctly versioned.

    If provided, `extra` must be the name of an pacakging extra (e.g. "saml2" in
    `pip install matrix-synapse[saml2]`).

    If `extra` is None, this function checks that
    - all mandatory dependencies are installed and correctly versioned, and
    - each optional dependency that's installed is correctly versioned.

    If `extra` is not None, this function checks that
    - the dependencies needed for that extra are installed and correctly versioned.

    :raises DependencyException: if a dependency is missing or incorrectly versioned.
    :raises ValueError: if this extra does not exist.
    """
    # First work out which dependencies are required, and which are optional.
    if extra is None:
        dependencies = _generic_dependencies()
    elif extra in RUNTIME_EXTRAS:
        dependencies = _dependencies_for_extra(extra)
    else:
        raise ValueError(f"Synapse {VERSION} does not provide the feature '{extra}'")

    deps_unfulfilled = []
    errors = []

    for requirement, must_be_installed in dependencies:
        try:
            dist: metadata.Distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            if must_be_installed:
                deps_unfulfilled.append(requirement.name)
                errors.append(_not_installed(requirement, extra))
        else:
            if dist.version is None:
                # This shouldn't happen---it suggests a borked virtualenv. (See
                # https://github.com/matrix-org/synapse/issues/12223)
                # Try to give a vaguely helpful error message anyway.
                # Type-ignore: the annotations don't reflect reality: see
                #     https://github.com/python/typeshed/issues/7513
                #     https://bugs.python.org/issue47060
                deps_unfulfilled.append(requirement.name)  # type: ignore[unreachable]
                errors.append(_no_reported_version(requirement, extra))

            # We specify prereleases=True to allow prereleases such as RCs.
            elif not requirement.specifier.contains(dist.version, prereleases=True):
                deps_unfulfilled.append(requirement.name)
                errors.append(_incorrect_version(requirement, dist.version, extra))

    if deps_unfulfilled:
        for err in errors:
            logger.error(err)

        raise DependencyException(deps_unfulfilled)
