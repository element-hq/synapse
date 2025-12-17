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
from typing import Any, Iterable, NamedTuple, Sequence, cast

from packaging.markers import Marker, Value, Variable, default_environment
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


def _marker_environment(extra: str) -> dict[str, str]:
    """Return the marker environment for `extra`, seeded with the current interpreter."""

    env = cast(dict[str, str], dict(default_environment()))
    env["extra"] = extra
    return env


def _is_dev_dependency(req: Requirement) -> bool:
    """Return True if `req` is a development dependency."""
    if req.marker is None:
        return False

    marker_extras = _extras_from_marker(req.marker)
    return any(
        extra in DEV_EXTRAS and req.marker.evaluate(_marker_environment(extra))
        for extra in marker_extras
    )


def _should_ignore_runtime_requirement(req: Requirement) -> bool:
    # This is a build-time dependency. Irritatingly, `poetry build` ignores the
    # requirements listed in the [build-system] section of pyproject.toml, so in order
    # to support `poetry install --without dev` we have to mark it as a runtime dependency.
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
    env_no_extra = _marker_environment("")
    for raw_requirement in requirements:
        req = Requirement(raw_requirement)
        if _is_dev_dependency(req) or _should_ignore_runtime_requirement(req):
            continue

        # https://packaging.pypa.io/en/latest/markers.html#usage notes that
        #   > Evaluating an extra marker with no environment is an error
        # so we pass in a dummy empty extra value here.
        must_be_installed = req.marker is None or req.marker.evaluate(env_no_extra)
        yield Dependency(req, must_be_installed)


def _dependencies_for_extra(extra: str) -> Iterable[Dependency]:
    """Yield additional dependencies needed for a given `extra`."""
    requirements = metadata.requires(DISTRIBUTION_NAME)
    assert requirements is not None
    env_no_extra = _marker_environment("")
    env_for_extra = _marker_environment(extra)
    for raw_requirement in requirements:
        req = Requirement(raw_requirement)
        if _is_dev_dependency(req):
            continue
        # Exclude mandatory deps by only selecting deps needed with this extra.
        if (
            req.marker is not None
            and req.marker.evaluate(env_for_extra)
            and not req.marker.evaluate(env_no_extra)
        ):
            yield Dependency(req, True)


def _values_from_marker_value(value: Value) -> set[str]:
    """Extract text values contained in a marker `Value`."""

    raw: Any = value.value
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, (tuple, list)):
        return {str(item) for item in raw}
    return {str(raw)}


def _extras_from_marker(marker: Marker | None) -> set[str]:
    """Return every `extra` referenced in the supplied marker tree."""

    extras: set[str] = set()

    if marker is None:
        return extras

    def collect(tree: object) -> None:
        if isinstance(tree, list):
            for item in tree:
                collect(item)
        elif isinstance(tree, tuple) and len(tree) == 3:
            lhs, _op, rhs = tree
            if (
                isinstance(lhs, Variable)
                and lhs.value == "extra"
                and isinstance(rhs, Value)
            ):
                extras.update(_values_from_marker_value(rhs))
            elif (
                isinstance(rhs, Variable)
                and rhs.value == "extra"
                and isinstance(lhs, Value)
            ):
                extras.update(_values_from_marker_value(lhs))

    collect(marker._markers)
    return extras


def _extras_to_consider_for_requirement(
    marker: Marker, base_candidates: Sequence[str]
) -> set[str]:
    """
    Augment `base_candidates` with extras explicitly mentioned in `marker`.

    Markers can mention extras (e.g. `extra == "saml2"`).
    """

    # Avoid modifying the input sequence.
    # Use a set to efficiently avoid duplicate extras.
    extras = set(base_candidates)

    for candidate in _extras_from_marker(marker):
        extras.add(candidate)

    return extras


def _marker_applies_for_any_extra(requirement: Requirement, extras: set[str]) -> bool:
    """Check whether a requirement's marker matches any evaluated `extra`."""

    if requirement.marker is None:
        return True

    return any(
        requirement.marker.evaluate(_marker_environment(extra)) for extra in extras
    )


def _not_installed(requirement: Requirement, extra: str | None = None) -> str:
    if extra:
        return (
            f"Synapse {VERSION} needs {requirement.name} for {extra}, "
            f"but it is not installed"
        )
    else:
        return f"Synapse {VERSION} needs {requirement.name}, but it is not installed"


def _incorrect_version(
    requirement: Requirement, got: str, extra: str | None = None
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


def _no_reported_version(requirement: Requirement, extra: str | None = None) -> str:
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


def check_requirements(extra: str | None = None) -> None:
    """Check Synapse's dependencies are present and correctly versioned.

    If provided, `extra` must be the name of an packaging extra (e.g. "saml2" in
    `pip install matrix-synapse[saml2]`).

    If `extra` is None, this function checks that
    - all mandatory dependencies are installed and correctly versioned, and
    - each optional dependency that's installed is correctly versioned.

    If `extra` is not None, this function checks that
    - the dependencies needed for that extra are installed and correctly versioned.

    `marker`s are optional attributes on each requirement which specify
    conditions under which the requirement applies. For example, a requirement
    might only be needed on Windows, or with Python < 3.14. Markers can
    additionally mention `extras` themselves, meaning a requirement may not
    apply if the marker mentions an extra that the user has not asked for.

    This function skips a requirement when its markers do not apply in the
    current environment.

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

    if extra is None:
        # Default to all mandatory dependencies (non-dev extras).
        # "" means all dependencies that aren't conditional on an extra.
        base_extra_candidates: Sequence[str] = ("", *RUNTIME_EXTRAS)
    else:
        base_extra_candidates = (extra,)

    for requirement, must_be_installed in dependencies:
        if requirement.marker is not None:
            candidate_extras = _extras_to_consider_for_requirement(
                requirement.marker, base_extra_candidates
            )
            # Skip checking this dependency if the requirement's marker object
            # (i.e. `python_version < "3.14" and os_name == "win32"`) does not
            # apply for any of the extras we're considering.
            if not _marker_applies_for_any_extra(requirement, candidate_extras):
                continue

        # Check if the requirement is installed and correctly versioned.
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
