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
"""
A script which enforces that Synapse always uses strict types when defining a Pydantic
model.

Pydantic does not yet offer a strict mode, but it is planned for pydantic v2. See

    https://github.com/pydantic/pydantic/issues/1098
    https://pydantic-docs.helpmanual.io/blog/pydantic-v2/#strict-mode

until then, this script is a best effort to stop us from introducing type coersion bugs
(like the infamous stringy power levels fixed in room version 10).
"""

import argparse
import contextlib
import functools
import importlib
import logging
import os
import pkgutil
import sys
import textwrap
import traceback
import unittest.mock
from contextlib import contextmanager
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Set,
    Type,
    TypeVar,
)

from parameterized import parameterized
from typing_extensions import ParamSpec

from synapse._pydantic_compat import (
    BaseModel as PydanticBaseModel,
    conbytes,
    confloat,
    conint,
    constr,
    get_args,
)

logger = logging.getLogger(__name__)

CONSTRAINED_TYPE_FACTORIES_WITH_STRICT_FLAG: List[Callable] = [
    constr,
    conbytes,
    conint,
    confloat,
]

TYPES_THAT_PYDANTIC_WILL_COERCE_TO = [
    str,
    bytes,
    int,
    float,
    bool,
]


P = ParamSpec("P")
R = TypeVar("R")


class ModelCheckerException(Exception):
    """Dummy exception. Allows us to detect unwanted types during a module import."""


class MissingStrictInConstrainedTypeException(ModelCheckerException):
    factory_name: str

    def __init__(self, factory_name: str):
        self.factory_name = factory_name


class FieldHasUnwantedTypeException(ModelCheckerException):
    message: str

    def __init__(self, message: str):
        self.message = message


def make_wrapper(factory: Callable[P, R]) -> Callable[P, R]:
    """We patch `constr` and friends with wrappers that enforce strict=True."""

    @functools.wraps(factory)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        if "strict" not in kwargs:
            raise MissingStrictInConstrainedTypeException(factory.__name__)
        if not kwargs["strict"]:
            raise MissingStrictInConstrainedTypeException(factory.__name__)
        return factory(*args, **kwargs)

    return wrapper


def field_type_unwanted(type_: Any) -> bool:
    """Very rough attempt to detect if a type is unwanted as a Pydantic annotation.

    At present, we exclude types which will coerce, or any generic type involving types
    which will coerce."""
    logger.debug("Is %s unwanted?")
    if type_ in TYPES_THAT_PYDANTIC_WILL_COERCE_TO:
        logger.debug("yes")
        return True
    logger.debug("Maybe. Subargs are %s", get_args(type_))
    rv = any(field_type_unwanted(t) for t in get_args(type_))
    logger.debug("Conclusion: %s %s unwanted", type_, "is" if rv else "is not")
    return rv


class PatchedBaseModel(PydanticBaseModel):
    """A patched version of BaseModel that inspects fields after models are defined.

    We complain loudly if we see an unwanted type.

    Beware: ModelField.type_ is presumably private; this is likely to be very brittle.
    """

    @classmethod
    def __init_subclass__(cls: Type[PydanticBaseModel], **kwargs: object):
        for field in cls.__fields__.values():
            # Note that field.type_ and field.outer_type are computed based on the
            # annotation type, see pydantic.fields.ModelField._type_analysis
            if field_type_unwanted(field.outer_type_):
                # TODO: this only reports the first bad field. Can we find all bad ones
                #  and report them all?
                raise FieldHasUnwantedTypeException(
                    f"{cls.__module__}.{cls.__qualname__} has field '{field.name}' "
                    f"with unwanted type `{field.outer_type_}`"
                )


@contextmanager
def monkeypatch_pydantic() -> Generator[None, None, None]:
    """Patch pydantic with our snooping versions of BaseModel and the con* functions.

    If the snooping functions see something they don't like, they'll raise a
    ModelCheckingException instance.
    """
    with contextlib.ExitStack() as patches:
        # Most Synapse code ought to import the patched objects directly from
        # `pydantic`. But we also patch their containing modules `pydantic.main` and
        # `pydantic.types` for completeness.
        patch_basemodel = unittest.mock.patch(
            "synapse._pydantic_compat.BaseModel", new=PatchedBaseModel
        )
        patches.enter_context(patch_basemodel)
        for factory in CONSTRAINED_TYPE_FACTORIES_WITH_STRICT_FLAG:
            wrapper: Callable = make_wrapper(factory)
            patch = unittest.mock.patch(
                f"synapse._pydantic_compat.{factory.__name__}", new=wrapper
            )
            patches.enter_context(patch)
        yield


def format_model_checker_exception(e: ModelCheckerException) -> str:
    """Work out which line of code caused e. Format the line in a human-friendly way."""
    # TODO. FieldHasUnwantedTypeException gives better error messages. Can we ditch the
    #   patches of constr() etc, and instead inspect fields to look for ConstrainedStr
    #   with strict=False? There is some difficulty with the inheritance hierarchy
    #   because StrictStr < ConstrainedStr < str.
    if isinstance(e, FieldHasUnwantedTypeException):
        return e.message
    elif isinstance(e, MissingStrictInConstrainedTypeException):
        frame_summary = traceback.extract_tb(e.__traceback__)[-2]
        return (
            f"Missing `strict=True` from {e.factory_name}() call \n"
            + traceback.format_list([frame_summary])[0].lstrip()
        )
    else:
        raise ValueError(f"Unknown exception {e}") from e


def lint() -> int:
    """Try to import all of Synapse and see if we spot any Pydantic type coercions.

    Print any problems, then return a status code suitable for sys.exit."""
    failures = do_lint()
    if failures:
        print(f"Found {len(failures)} problem(s)")
    for failure in sorted(failures):
        print(failure)
    return os.EX_DATAERR if failures else os.EX_OK


def do_lint() -> Set[str]:
    """Try to import all of Synapse and see if we spot any Pydantic type coercions."""
    failures = set()

    with monkeypatch_pydantic():
        logger.debug("Importing synapse")
        try:
            # TODO: make "synapse" an argument so we can target this script at
            # a subpackage
            module = importlib.import_module("synapse")
        except ModelCheckerException as e:
            logger.warning("Bad annotation found when importing synapse")
            failures.add(format_model_checker_exception(e))
            return failures

        try:
            logger.debug("Fetching subpackages")
            module_infos = list(
                pkgutil.walk_packages(module.__path__, f"{module.__name__}.")
            )
        except ModelCheckerException as e:
            logger.warning("Bad annotation found when looking for modules to import")
            failures.add(format_model_checker_exception(e))
            return failures

        for module_info in module_infos:
            logger.debug("Importing %s", module_info.name)
            try:
                importlib.import_module(module_info.name)
            except ModelCheckerException as e:
                logger.warning(
                    f"Bad annotation found when importing {module_info.name}"
                )
                failures.add(format_model_checker_exception(e))

    return failures


def run_test_snippet(source: str) -> None:
    """Exec a snippet of source code in an isolated environment."""
    # To emulate `source` being called at the top level of the module,
    # the globals and locals we provide apparently have to be the same mapping.
    #
    # > Remember that at the module level, globals and locals are the same dictionary.
    # > If exec gets two separate objects as globals and locals, the code will be
    # > executed as if it were embedded in a class definition.
    globals_: Dict[str, object]
    locals_: Dict[str, object]
    globals_ = locals_ = {}
    exec(textwrap.dedent(source), globals_, locals_)


class TestConstrainedTypesPatch(unittest.TestCase):
    def test_expression_without_strict_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import constr
                except ImportError:
                    from pydantic import constr
                constr()
                """
            )

    def test_called_as_module_attribute_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                import pydantic
                pydantic.constr()
                """
            )

    def test_wildcard_import_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import *
                except ImportError:
                    from pydantic import *
                constr()
                """
            )

    def test_alternative_import_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1.types import constr
                except ImportError:
                    from pydantic.types import constr
                constr()
                """
            )

    def test_alternative_import_attribute_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import types as pydantic_types
                except ImportError:
                    from pydantic import types as pydantic_types
                pydantic_types.constr()
                """
            )

    def test_kwarg_but_no_strict_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import constr
                except ImportError:
                    from pydantic import constr
                constr(min_length=10)
                """
            )

    def test_kwarg_strict_False_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import constr
                except ImportError:
                    from pydantic import constr
                constr(strict=False)
                """
            )

    def test_kwarg_strict_True_doesnt_raise(self) -> None:
        with monkeypatch_pydantic():
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import constr
                except ImportError:
                    from pydantic import constr
                constr(strict=True)
                """
            )

    def test_annotation_without_strict_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import constr
                except ImportError:
                    from pydantic import constr
                x: constr()
                """
            )

    def test_field_annotation_without_strict_raises(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1 import BaseModel, conint
                except ImportError:
                    from pydantic import BaseModel, conint
                class C:
                    x: conint()
                """
            )


class TestFieldTypeInspection(unittest.TestCase):
    @parameterized.expand(
        [
            ("str",),
            ("bytes"),
            ("int",),
            ("float",),
            ("bool"),
            ("Optional[str]",),
            ("Union[None, str]",),
            ("List[str]",),
            ("List[List[str]]",),
            ("Dict[StrictStr, str]",),
            ("Dict[str, StrictStr]",),
            ("TypedDict('D', x=int)",),
        ]
    )
    def test_field_holding_unwanted_type_raises(self, annotation: str) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                f"""
                from typing import *
                try:
                    from pydantic.v1 import *
                except ImportError:
                    from pydantic import *
                class C(BaseModel):
                    f: {annotation}
                """
            )

    @parameterized.expand(
        [
            ("StrictStr",),
            ("StrictBytes"),
            ("StrictInt",),
            ("StrictFloat",),
            ("StrictBool"),
            ("constr(strict=True, min_length=10)",),
            ("Optional[StrictStr]",),
            ("Union[None, StrictStr]",),
            ("List[StrictStr]",),
            ("List[List[StrictStr]]",),
            ("Dict[StrictStr, StrictStr]",),
            ("TypedDict('D', x=StrictInt)",),
        ]
    )
    def test_field_holding_accepted_type_doesnt_raise(self, annotation: str) -> None:
        with monkeypatch_pydantic():
            run_test_snippet(
                f"""
                from typing import *
                try:
                    from pydantic.v1 import *
                except ImportError:
                    from pydantic import *
                class C(BaseModel):
                    f: {annotation}
                """
            )

    def test_field_holding_str_raises_with_alternative_import(self) -> None:
        with monkeypatch_pydantic(), self.assertRaises(ModelCheckerException):
            run_test_snippet(
                """
                try:
                    from pydantic.v1.main import BaseModel
                except ImportError:
                    from pydantic.main import BaseModel
                class C(BaseModel):
                    f: str
                """
            )


parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["lint", "test"], default="lint", nargs="?")
parser.add_argument("-v", "--verbose", action="store_true")


if __name__ == "__main__":
    args = parser.parse_args(sys.argv[1:])
    logging.basicConfig(
        format="%(asctime)s %(name)s:%(lineno)d %(levelname)s %(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )
    # suppress logs we don't care about
    logging.getLogger("xmlschema").setLevel(logging.WARNING)
    if args.mode == "lint":
        sys.exit(lint())
    elif args.mode == "test":
        unittest.main(argv=sys.argv[:1])
