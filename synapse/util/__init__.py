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
import logging
import typing
from typing import (
    Iterator,
    Mapping,
    Sequence,
    TypeVar,
)

import attr
from matrix_common.versionstring import get_distribution_version_string

from twisted.internet import defer
from twisted.python.failure import Failure

if typing.TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class Duration:
    """Helper class that holds constants for common time durations in
    milliseconds."""

    MINUTE_MS = 60 * 1000
    HOUR_MS = 60 * MINUTE_MS
    DAY_MS = 24 * HOUR_MS


def unwrapFirstError(failure: Failure) -> Failure:
    # Deprecated: you probably just want to catch defer.FirstError and reraise
    # the subFailure's value, which will do a better job of preserving stacktraces.
    # (actually, you probably want to use yieldable_gather_results anyway)
    failure.trap(defer.FirstError)
    return failure.value.subFailure


def log_failure(
    failure: Failure, msg: str, consumeErrors: bool = True
) -> Failure | None:
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
    _mutable_map: dict[K, V] = attr.ib(factory=dict)
    _deletions: set[K] = attr.ib(factory=set)

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
