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
import os
import typing
from typing import (
    Any,
    Iterator,
    Mapping,
    Sequence,
    TypeVar,
)

import attr
from canonicaljson import encode_canonical_json
from matrix_common.versionstring import get_distribution_version_string

from twisted.internet import defer
from twisted.python.failure import Failure

from synapse.types import JsonDict

if typing.TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


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


SYNAPSE_VERSION = os.getenv(
    "SYNAPSE_VERSION_STRING"
) or get_distribution_version_string("matrix-synapse", __file__)
"""
Version string with git info.

This can be overridden via the `SYNAPSE_VERSION_STRING` environment variable or is
computed here once so that we don't invoke git multiple times.
"""


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


@attr.s(slots=True, auto_attribs=True)
class _DictSplitterState:
    """State for splitting a dict into multiple dicts, c.f.
    `split_dict_to_fit_to_size`."""

    subset: dict[str, Any]
    """A subset of the original dict."""

    estimated_size: int
    """Estimated size of the JSON encoding of the current payload, including any
    wrapping structure."""


def split_dict_to_fit_to_size(
    original_dict: dict[str, Any],
    *,
    soft_max_size: int,
    wrapping_object_size: int = 2,
) -> Iterator[tuple[dict[str, JsonDict], int]]:
    """Splits a dict up into a list of dicts, each of which is small enough to
    fit into the given size when encoded as JSON. Every entry in the original
    dict is in exactly one of the resulting dicts.

    The `wrapping_object_size` can be used if the resulting dicts are going to
    be wrapped in some additional JSON structure, to account for the additional
    size of that structure. The default assumes no wrapping, and just accounts
    for the two curly braces of the dict itself.

    Note that if an individual entry in the original dict is larger than
    `soft_max_size` then this will emit a dict containing just that entry, which
    will be larger than `soft_max_size` when encoded as JSON.

    Args:
        original_dict: The dict to split.
        soft_max_size: The maximum size of each dict when encoded as JSON.
        wrapping_object_size: The estimated size of the JSON encoding of the
            payload when empty.

    Returns:
        An iterator of (dict, size) pairs, where dict is a subset of the
        original dict and size is the estimated size of the JSON encoding of
        that dict, including any wrapping structure.
    """

    if not original_dict:
        return

    # Check if the whole dict fits within the size limit. If it does, we can
    # skip the splitting logic and just return the original dict.
    full_size = _len_with_wrapping_object(original_dict, wrapping_object_size)
    if full_size <= soft_max_size:
        yield (original_dict, full_size)
        return

    # The current payload being built up. We keep track of the estimated size of
    # the JSON encoding of this payload so that we can decide when to start a
    # new one.
    current_payload = _DictSplitterState(subset={}, estimated_size=wrapping_object_size)

    for key, payload in original_dict.items():
        current_payload.subset[key] = payload
        current_size = _len_with_wrapping_object(
            current_payload.subset, wrapping_object_size
        )

        if current_size > soft_max_size:
            # We've exceeded the size limit, so we need to start a new payload. We pop
            # the current entry from the payload and yield the previous payload, then
            # start a new payload with just the current entry.
            if len(current_payload.subset) > 1:
                current_payload.subset.pop(key)
                yield current_payload.subset, current_payload.estimated_size

                current_payload = _DictSplitterState(
                    subset={},
                    estimated_size=wrapping_object_size,
                )

                # Recalculate the current size with just the current entry.
                current_size = _len_with_wrapping_object(
                    {key: payload}, wrapping_object_size
                )

        current_payload.subset[key] = payload
        current_payload.estimated_size = current_size

    if current_payload.subset:
        # yield the final payload if it's non-empty
        yield current_payload.subset, current_payload.estimated_size


def _len_with_wrapping_object(payload: Any, wrapping_object_size: int) -> int:
    """Helper function to calculate the size of a payload when encoded as JSON,
    including any wrapping structure."""
    return (
        len(encode_canonical_json(payload))
        + wrapping_object_size
        # account for the curly braces of the dict itself, which are
        # included in the size of the subset but not in the size of the
        # payload
        - 2
    )
