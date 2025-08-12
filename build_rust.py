# A build script for poetry that adds the rust extension.

import itertools
import os
from typing import Any, Dict

from packaging.specifiers import SpecifierSet
from setuptools_rust import Binding, RustExtension


def build(setup_kwargs: Dict[str, Any]) -> None:
    original_project_dir = os.path.dirname(os.path.realpath(__file__))
    cargo_toml_path = os.path.join(original_project_dir, "rust", "Cargo.toml")

    extension = RustExtension(
        target="synapse.synapse_rust",
        path=cargo_toml_path,
        binding=Binding.PyO3,
        # This flag is a no-op in the latest versions. Instead, we need to
        # specify this in the `bdist_wheel` config below.
        py_limited_api=True,
        # We force always building in release mode, as we can't tell the
        # difference between using `poetry` in development vs production.
        debug=False,
    )
    setup_kwargs.setdefault("rust_extensions", []).append(extension)
    setup_kwargs["zip_safe"] = False

    # We lookup the minimum supported python version by looking at
    # `python_requires` (e.g. ">=3.9.0,<4.0.0") and finding the first python
    # version that matches. We then convert that into the `py_limited_api` form,
    # e.g. cp39 for python 3.9.
    py_limited_api: str
    python_bounds = SpecifierSet(setup_kwargs["python_requires"])
    for minor_version in itertools.count(start=8):
        if f"3.{minor_version}.0" in python_bounds:
            py_limited_api = f"cp3{minor_version}"
            break

    setup_kwargs.setdefault("options", {}).setdefault("bdist_wheel", {})[
        "py_limited_api"
    ] = py_limited_api
