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

import functools
import sys
from typing import Any, Callable, Generator, List, TypeVar, cast

from typing_extensions import ParamSpec

from twisted.internet import defer
from twisted.internet.defer import Deferred
from twisted.python.failure import Failure

# Tracks if we've already patched inlineCallbacks
_already_patched = False


T = TypeVar("T")
P = ParamSpec("P")


def do_patch() -> None:
    """
    Patch defer.inlineCallbacks so that it checks the state of the logcontext on exit
    """

    from synapse.logging.context import current_context

    global _already_patched

    orig_inline_callbacks = defer.inlineCallbacks
    if _already_patched:
        return

    def new_inline_callbacks(
        f: Callable[P, Generator["Deferred[object]", object, T]],
    ) -> Callable[P, "Deferred[T]"]:
        @functools.wraps(f)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> "Deferred[T]":
            start_context = current_context()
            changes: List[str] = []
            orig: Callable[P, "Deferred[T]"] = orig_inline_callbacks(
                _check_yield_points(f, changes)
            )

            try:
                res: "Deferred[T]" = orig(*args, **kwargs)
            except Exception:
                if current_context() != start_context:
                    for err in changes:
                        print(err, file=sys.stderr)

                    err = "%s changed context from %s to %s on exception" % (
                        f,
                        start_context,
                        current_context(),
                    )
                    print(err, file=sys.stderr)
                    raise Exception(err)
                raise

            if not isinstance(res, Deferred) or res.called:
                if current_context() != start_context:
                    for err in changes:
                        print(err, file=sys.stderr)

                    err = "Completed %s changed context from %s to %s" % (
                        f,
                        start_context,
                        current_context(),
                    )
                    # print the error to stderr because otherwise all we
                    # see in travis-ci is the 500 error
                    print(err, file=sys.stderr)
                    raise Exception(err)
                return res

            if current_context():
                err = (
                    "%s returned incomplete deferred in non-sentinel context "
                    "%s (start was %s)"
                ) % (f, current_context(), start_context)
                print(err, file=sys.stderr)
                raise Exception(err)

            def check_ctx(r: T) -> T:
                if current_context() != start_context:
                    for err in changes:
                        print(err, file=sys.stderr)
                    err = "%s completion of %s changed context from %s to %s" % (
                        "Failure" if isinstance(r, Failure) else "Success",
                        f,
                        start_context,
                        current_context(),
                    )
                    print(err, file=sys.stderr)
                    raise Exception(err)
                return r

            res.addBoth(check_ctx)
            return res

        return wrapped

    defer.inlineCallbacks = new_inline_callbacks
    _already_patched = True


def _check_yield_points(
    f: Callable[P, Generator["Deferred[object]", object, T]],
    changes: List[str],
) -> Callable:
    """Wraps a generator that is about to be passed to defer.inlineCallbacks
    checking that after every yield the log contexts are correct.

    It's perfectly valid for log contexts to change within a function, e.g. due
    to new Measure blocks, so such changes are added to the given `changes`
    list instead of triggering an exception.

    Args:
        f: generator function to wrap
        changes: A list of strings detailing how the contexts
            changed within a function.

    Returns:
        function
    """

    from synapse.logging.context import current_context

    @functools.wraps(f)
    def check_yield_points_inner(
        *args: P.args, **kwargs: P.kwargs
    ) -> Generator["Deferred[object]", object, T]:
        gen = f(*args, **kwargs)

        last_yield_line_no = gen.gi_frame.f_lineno
        result: Any = None
        while True:
            expected_context = current_context()

            try:
                isFailure = isinstance(result, Failure)
                if isFailure:
                    d = result.throwExceptionIntoGenerator(gen)
                else:
                    d = gen.send(result)
            except (StopIteration, defer._DefGen_Return) as e:
                if current_context() != expected_context:
                    # This happens when the context is lost sometime *after* the
                    # final yield and returning. E.g. we forgot to yield on a
                    # function that returns a deferred.
                    #
                    # We don't raise here as it's perfectly valid for contexts to
                    # change in a function, as long as it sets the correct context
                    # on resolving (which is checked separately).
                    err = (
                        "Function %r returned and changed context from %s to %s,"
                        " in %s between %d and end of func"
                        % (
                            f.__qualname__,
                            expected_context,
                            current_context(),
                            f.__code__.co_filename,
                            last_yield_line_no,
                        )
                    )
                    changes.append(err)
                # The `StopIteration` or `_DefGen_Return` contains the return value from the
                # generator.
                return cast(T, e.value)

            frame = gen.gi_frame

            if isinstance(d, defer.Deferred) and not d.called:
                # This happens if we yield on a deferred that doesn't follow
                # the log context rules without wrapping in a `make_deferred_yieldable`.
                # We raise here as this should never happen.
                if current_context():
                    err = (
                        "%s yielded with context %s rather than sentinel,"
                        " yielded on line %d in %s"
                        % (
                            frame.f_code.co_name,
                            current_context(),
                            frame.f_lineno,
                            frame.f_code.co_filename,
                        )
                    )
                    raise Exception(err)

            # the wrapped function yielded a Deferred: yield it back up to the parent
            # inlineCallbacks().
            try:
                result = yield d
            except Exception:
                # this will fish an earlier Failure out of the stack where possible, and
                # thus is preferable to passing in an exception to the Failure
                # constructor, since it results in less stack-mangling.
                result = Failure()

            if current_context() != expected_context:
                # This happens because the context is lost sometime *after* the
                # previous yield and *after* the current yield. E.g. the
                # deferred we waited on didn't follow the rules, or we forgot to
                # yield on a function between the two yield points.
                #
                # We don't raise here as its perfectly valid for contexts to
                # change in a function, as long as it sets the correct context
                # on resolving (which is checked separately).
                err = (
                    "%s changed context from %s to %s, happened between lines %d and %d in %s"
                    % (
                        frame.f_code.co_name,
                        expected_context,
                        current_context(),
                        last_yield_line_no,
                        frame.f_lineno,
                        frame.f_code.co_filename,
                    )
                )
                changes.append(err)

            last_yield_line_no = frame.f_lineno

    return check_yield_points_inner
