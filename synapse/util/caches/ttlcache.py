#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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

import logging
import time
from typing import Any, Callable, Generic, TypeVar

import attr
from sortedcontainers import SortedList

from synapse.util.caches import register_cache

logger = logging.getLogger(__name__)

SENTINEL: Any = object()

T = TypeVar("T")
KT = TypeVar("KT")
VT = TypeVar("VT")


class TTLCache(Generic[KT, VT]):
    """A key/value cache implementation where each entry has its own TTL"""

    def __init__(
        self,
        *,
        cache_name: str,
        server_name: str,
        timer: Callable[[], float] = time.time,
    ):
        """
        Args:
            cache_name
            server_name: The homeserver name that this cache is associated with
                (used to label the metric) (`hs.hostname`).
            timer: Function used to get the current time in seconds since the epoch.
        """

        # map from key to _CacheEntry
        self._data: dict[KT, _CacheEntry[KT, VT]] = {}

        # the _CacheEntries, sorted by expiry time
        self._expiry_list: SortedList[_CacheEntry[KT, VT]] = SortedList()

        self._timer = timer

        self._metrics = register_cache(
            cache_type="ttl",
            cache_name=cache_name,
            cache=self,
            server_name=server_name,
            resizable=False,
        )

    def set(self, key: KT, value: VT, ttl: float) -> None:
        """Add/update an entry in the cache

        Args:
            key: key for this entry
            value: value for this entry
            ttl: TTL for this entry, in seconds
        """
        expiry = self._timer() + ttl

        self.expire()
        e = self._data.pop(key, SENTINEL)
        if e is not SENTINEL:
            assert isinstance(e, _CacheEntry)
            self._expiry_list.remove(e)

        entry = _CacheEntry(expiry_time=expiry, ttl=ttl, key=key, value=value)
        self._data[key] = entry
        self._expiry_list.add(entry)

    def get(self, key: KT, default: T = SENTINEL) -> VT | T:
        """Get a value from the cache

        Args:
            key: key to look up
            default: default value to return, if key is not found. If not set, and the
                key is not found, a KeyError will be raised

        Returns:
            value from the cache, or the default
        """
        self.expire()
        e = self._data.get(key, SENTINEL)
        if e is SENTINEL:
            self._metrics.inc_misses()
            if default is SENTINEL:
                raise KeyError(key)
            return default
        assert isinstance(e, _CacheEntry)
        self._metrics.inc_hits()
        return e.value

    def get_with_expiry(self, key: KT) -> tuple[VT, float, float]:
        """Get a value, and its expiry time, from the cache

        Args:
            key: key to look up

        Returns:
            A tuple of  the value from the cache, the expiry time and the TTL

        Raises:
            KeyError if the entry is not found
        """
        self.expire()
        try:
            e = self._data[key]
        except KeyError:
            self._metrics.inc_misses()
            raise
        self._metrics.inc_hits()
        return e.value, e.expiry_time, e.ttl

    def pop(self, key: KT, default: T = SENTINEL) -> VT | T:
        """Remove a value from the cache

        If key is in the cache, remove it and return its value, else return default.
        If default is not given and key is not in the cache, a KeyError is raised.

        Args:
            key: key to look up
            default: default value to return, if key is not found. If not set, and the
                key is not found, a KeyError will be raised

        Returns:
            value from the cache, or the default
        """
        self.expire()
        e = self._data.pop(key, SENTINEL)
        if e is SENTINEL:
            self._metrics.inc_misses()
            if default is SENTINEL:
                raise KeyError(key)
            return default
        assert isinstance(e, _CacheEntry)
        self._expiry_list.remove(e)
        self._metrics.inc_hits()
        return e.value

    def __getitem__(self, key: KT) -> VT:
        return self.get(key)

    def __delitem__(self, key: KT) -> None:
        self.pop(key)

    def __contains__(self, key: KT) -> bool:
        return key in self._data

    def __len__(self) -> int:
        self.expire()
        return len(self._data)

    def expire(self) -> None:
        """Run the expiry on the cache. Any entries whose expiry times are due will
        be removed
        """
        now = self._timer()
        while self._expiry_list:
            first_entry = self._expiry_list[0]
            if first_entry.expiry_time - now > 0.0:
                break
            del self._data[first_entry.key]
            del self._expiry_list[0]


@attr.s(frozen=True, slots=True, auto_attribs=True)
class _CacheEntry(Generic[KT, VT]):
    """TTLCache entry"""

    # expiry_time is the first attribute, so that entries are sorted by expiry.
    expiry_time: float
    ttl: float
    key: KT
    value: VT
