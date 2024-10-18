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

from contextlib import contextmanager
from os import PathLike
from pathlib import Path
from typing import Generator, Optional, Union
from unittest.mock import patch

from synapse.util.check_dependencies import (
    DependencyException,
    check_requirements,
    metadata,
)

from tests.unittest import TestCase


class DummyDistribution(metadata.Distribution):
    def __init__(self, version: str):
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def locate_file(self, path: Union[str, PathLike]) -> Path:
        raise NotImplementedError()

    def read_text(self, filename: str) -> None:
        raise NotImplementedError()


old = DummyDistribution("0.1.2")
old_release_candidate = DummyDistribution("0.1.2rc3")
new = DummyDistribution("1.2.3")
new_release_candidate = DummyDistribution("1.2.3rc4")
distribution_with_no_version = DummyDistribution(None)  # type: ignore[arg-type]

# could probably use stdlib TestCase --- no need for twisted here


class TestDependencyChecker(TestCase):
    @contextmanager
    def mock_installed_package(
        self, distribution: Optional[DummyDistribution]
    ) -> Generator[None, None, None]:
        """Pretend that looking up any package yields the given `distribution`.

        If `distribution = None`, we pretend that the package is not installed.
        """

        def mock_distribution(name: str) -> DummyDistribution:
            if distribution is None:
                raise metadata.PackageNotFoundError
            else:
                return distribution

        with patch(
            "synapse.util.check_dependencies.metadata.distribution",
            mock_distribution,
        ):
            yield

    def test_mandatory_dependency(self) -> None:
        """Complain if a required package is missing or old."""
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["dummypkg >= 1"],
        ):
            with self.mock_installed_package(None):
                self.assertRaises(DependencyException, check_requirements)
            with self.mock_installed_package(old):
                self.assertRaises(DependencyException, check_requirements)
            with self.mock_installed_package(new):
                # should not raise
                check_requirements()

    def test_version_reported_as_none(self) -> None:
        """Complain if importlib.metadata.version() returns None.

        This shouldn't normally happen, but it was seen in the wild
        (https://github.com/matrix-org/synapse/issues/12223).
        """
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["dummypkg >= 1"],
        ):
            with self.mock_installed_package(distribution_with_no_version):
                self.assertRaises(DependencyException, check_requirements)

    def test_checks_ignore_dev_dependencies(self) -> None:
        """Both generic and per-extra checks should ignore dev dependencies."""
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["dummypkg >= 1; extra == 'mypy'"],
        ), patch("synapse.util.check_dependencies.RUNTIME_EXTRAS", {"cool-extra"}):
            # We're testing that none of these calls raise.
            with self.mock_installed_package(None):
                check_requirements()
                check_requirements("cool-extra")
            with self.mock_installed_package(old):
                check_requirements()
                check_requirements("cool-extra")
            with self.mock_installed_package(new):
                check_requirements()
                check_requirements("cool-extra")

    def test_generic_check_of_optional_dependency(self) -> None:
        """Complain if an optional package is old."""
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["dummypkg >= 1; extra == 'cool-extra'"],
        ):
            with self.mock_installed_package(None):
                # should not raise
                check_requirements()
            with self.mock_installed_package(old):
                self.assertRaises(DependencyException, check_requirements)
            with self.mock_installed_package(new):
                # should not raise
                check_requirements()

    def test_check_for_extra_dependencies(self) -> None:
        """Complain if a package required for an extra is missing or old."""
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["dummypkg >= 1; extra == 'cool-extra'"],
        ), patch("synapse.util.check_dependencies.RUNTIME_EXTRAS", {"cool-extra"}):
            with self.mock_installed_package(None):
                self.assertRaises(DependencyException, check_requirements, "cool-extra")
            with self.mock_installed_package(old):
                self.assertRaises(DependencyException, check_requirements, "cool-extra")
            with self.mock_installed_package(new):
                # should not raise
                check_requirements("cool-extra")

    def test_release_candidates_satisfy_dependency(self) -> None:
        """
        Tests that release candidates count as far as satisfying a dependency
        is concerned.
        (Regression test, see https://github.com/matrix-org/synapse/issues/12176.)
        """
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["dummypkg >= 1"],
        ):
            with self.mock_installed_package(old_release_candidate):
                self.assertRaises(DependencyException, check_requirements)

            with self.mock_installed_package(new_release_candidate):
                # should not raise
                check_requirements()

    def test_setuptools_rust_ignored(self) -> None:
        """
        Test a workaround for a `poetry build` problem. Reproduces
        https://github.com/matrix-org/synapse/issues/13926.
        """
        with patch(
            "synapse.util.check_dependencies.metadata.requires",
            return_value=["setuptools_rust >= 1.3"],
        ):
            with self.mock_installed_package(None):
                # should not raise, even if setuptools_rust is not installed
                check_requirements()
            with self.mock_installed_package(old):
                # We also ignore old versions of setuptools_rust
                check_requirements()
