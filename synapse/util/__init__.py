#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

import collections.abc
import json
import logging
import typing
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    Mapping,
    Optional,
    Sequence,
    Set,
    TypeVar,
)

import attr
from immutabledict import immutabledict
from matrix_common.versionstring import get_distribution_version_string
from typing_extensions import ParamSpec

from twisted.internet import defer, task
from twisted.internet.defer import Deferred
from twisted.internet.interfaces import IDelayedCall, IReactorTime
from twisted.internet.task import LoopingCall
from twisted.python.failure import Failure

from synapse.logging import context

if typing.TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class Duration:
    """Helper class that holds constants for common time durations in
    milliseconds."""

    MINUTE_MS = 60 * 1000
    HOUR_MS = 60 * MINUTE_MS
    DAY_MS = 24 * HOUR_MS


def _reject_invalid_json(val: Any) -> None:
    """Do not allow Infinity, -Infinity, or NaN values in JSON."""
    raise ValueError("Invalid JSON value: '%s'" % val)


def _handle_immutabledict(obj: Any) -> Dict[Any, Any]:
    """Helper for json_encoder. Makes immutabledicts serializable by returning
    the underlying dict
    """
    if type(obj) is immutabledict:
        # fishing the protected dict out of the object is a bit nasty,
        # but we don't really want the overhead of copying the dict.
        try:
            # Safety: we catch the AttributeError immediately below.
            return obj._dict
        except AttributeError:
            # If all else fails, resort to making a copy of the immutabledict
            return dict(obj)
    raise TypeError(
        "Object of type %s is not JSON serializable" % obj.__class__.__name__
    )


# A custom JSON encoder which:
#   * handles immutabledicts
#   * produces valid JSON (no NaNs etc)
#   * reduces redundant whitespace
json_encoder = json.JSONEncoder(
    allow_nan=False, separators=(",", ":"), default=_handle_immutabledict
)

# Create a custom decoder to reject Python extensions to JSON.
json_decoder = json.JSONDecoder(parse_constant=_reject_invalid_json)


def unwrapFirstError(failure: Failure) -> Failure:
    # Deprecated: you probably just want to catch defer.FirstError and reraise
    # the subFailure's value, which will do a better job of preserving stacktraces.
    # (actually, you probably want to use yieldable_gather_results anyway)
    failure.trap(defer.FirstError)
    return failure.value.subFailure


P = ParamSpec("P")


@attr.s(slots=True)
class Clock:
    """
    A Clock wraps a Twisted reactor and provides utilities on top of it.

    Args:
        reactor: The Twisted reactor to use.
    """

    _reactor: IReactorTime = attr.ib()

    @defer.inlineCallbacks
    def sleep(self, seconds: float) -> "Generator[Deferred[float], Any, Any]":
        d: defer.Deferred[float] = defer.Deferred()
        with context.PreserveLoggingContext():
            self._reactor.callLater(seconds, d.callback, seconds)
            res = yield d
        return res

    def time(self) -> float:
        """Returns the current system time in seconds since epoch."""
        return self._reactor.seconds()

    def time_msec(self) -> int:
        """Returns the current system time in milliseconds since epoch."""
        return int(self.time() * 1000)

    def looping_call(
        self,
        f: Callable[P, object],
        msec: float,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Call a function repeatedly.

        Waits `msec` initially before calling `f` for the first time.

        If the function given to `looping_call` returns an awaitable/deferred, the next
        call isn't scheduled until after the returned awaitable has finished. We get
        this functionality thanks to this function being a thin wrapper around
        `twisted.internet.task.LoopingCall`.

        Note that the function will be called with no logcontext, so if it is anything
        other than trivial, you probably want to wrap it in run_as_background_process.

        Args:
            f: The function to call repeatedly.
            msec: How long to wait between calls in milliseconds.
            *args: Positional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        return self._looping_call_common(f, msec, False, *args, **kwargs)

    def looping_call_now(
        self,
        f: Callable[P, object],
        msec: float,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Call a function immediately, and then repeatedly thereafter.

        As with `looping_call`: subsequent calls are not scheduled until after the
        the Awaitable returned by a previous call has finished.

        Also as with `looping_call`: the function is called with no logcontext and
        you probably want to wrap it in `run_as_background_process`.

        Args:
            f: The function to call repeatedly.
            msec: How long to wait between calls in milliseconds.
            *args: Positional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """
        return self._looping_call_common(f, msec, True, *args, **kwargs)

    def _looping_call_common(
        self,
        f: Callable[P, object],
        msec: float,
        now: bool,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> LoopingCall:
        """Common functionality for `looping_call` and `looping_call_now`"""
        call = task.LoopingCall(f, *args, **kwargs)
        call.clock = self._reactor
        d = call.start(msec / 1000.0, now=now)
        d.addErrback(log_failure, "Looping call died", consumeErrors=False)
        return call

    def call_later(
        self, delay: float, callback: Callable, *args: Any, **kwargs: Any
    ) -> IDelayedCall:
        """Call something later

        Note that the function will be called with no logcontext, so if it is anything
        other than trivial, you probably want to wrap it in run_as_background_process.

        Args:
            delay: How long to wait in seconds.
            callback: Function to call
            *args: Postional arguments to pass to function.
            **kwargs: Key arguments to pass to function.
        """

        def wrapped_callback(*args: Any, **kwargs: Any) -> None:
            with context.PreserveLoggingContext():
                callback(*args, **kwargs)

        with context.PreserveLoggingContext():
            return self._reactor.callLater(delay, wrapped_callback, *args, **kwargs)

    def cancel_call_later(self, timer: IDelayedCall, ignore_errs: bool = False) -> None:
        try:
            timer.cancel()
        except Exception:
            if not ignore_errs:
                raise


def log_failure(
    failure: Failure, msg: str, consumeErrors: bool = True
) -> Optional[Failure]:
    """Creates a function suitable for passing to `Deferred.addErrback` that
    logs any failures that occur.

    Args:
        failure: The Failure to log
        msg: Message to log
        consumeErrors: If true consumes the failure, otherwise passes on down
            the callback chain

    Returns:
        The Failure if consumeErrors is false. None, otherwise.
    """

    logger.error(
        msg, exc_info=(failure.type, failure.value, failure.getTracebackObject())
    )

    if not consumeErrors:
        return failure
    return None


# Version string with git info. Computed here once so that we don't invoke git multiple
# times.
SYNAPSE_VERSION = get_distribution_version_string("matrix-synapse", __file__)


class ExceptionBundle(Exception):
    # A poor stand-in for something like Python 3.11's ExceptionGroup.
    # (A backport called `exceptiongroup` exists but seems overkill: we just want a
    # container type here.)
    def __init__(self, message: str, exceptions: Sequence[Exception]):
        parts = [message]
        for e in exceptions:
            parts.append(str(e))
        super().__init__("\n  - ".join(parts))
        self.exceptions = exceptions


K = TypeVar("K")
V = TypeVar("V")


@attr.s(slots=True, auto_attribs=True)
class MutableOverlayMapping(collections.abc.MutableMapping[K, V]):
    """A mutable mapping that allows changes to a read-only underlying
    mapping. Supports deletions.

    This is useful for cases where you want to allow modifications to a mapping
    without changing or copying the original mapping.

    Note: the underlying mapping must not change while this proxy is in use.
    """

    _underlying_map: Mapping[K, V]
    _mutable_map: Dict[K, V] = attr.ib(factory=dict)
    _deletions: Set[K] = attr.ib(factory=set)

    def __getitem__(self, key: K) -> V:
        if key in self._deletions:
            raise KeyError(key)
        if key in self._mutable_map:
            return self._mutable_map[key]
        return self._underlying_map[key]

    def __setitem__(self, key: K, value: V) -> None:
        self._deletions.discard(key)
        self._mutable_map[key] = value

    def __delitem__(self, key: K) -> None:
        if key not in self:
            raise KeyError(key)

        self._deletions.add(key)
        self._mutable_map.pop(key, None)

    def __iter__(self) -> Iterator[K]:
        for key in self._mutable_map:
            if key not in self._deletions:
                yield key

        for key in self._underlying_map:
            if key not in self._deletions and key not in self._mutable_map:
                # `key` should not be in both _mutable_map and _deletions
                assert key not in self._mutable_map
                yield key

    def __len__(self) -> int:
        count = len(self._underlying_map)
        for key in self._deletions:
            if key in self._underlying_map:
                count -= 1

        for key in self._mutable_map:
            # `key` should not be in both _mutable_map and _deletions
            assert key not in self._deletions

            if key not in self._underlying_map:
                count += 1

        return count

    def clear(self) -> None:
        self._underlying_map = {}
        self._mutable_map.clear()
        self._deletions.clear()
