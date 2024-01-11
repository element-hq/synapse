#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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
from typing import Generic, Hashable, List, Set, TypeVar

import attr

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=Hashable)


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _Entry(Generic[T]):
    end_key: int
    elements: Set[T] = attr.Factory(set)


class WheelTimer(Generic[T]):
    """Stores arbitrary objects that will be returned after their timers have
    expired.
    """

    def __init__(self, bucket_size: int = 5000) -> None:
        """
        Args:
            bucket_size: Size of buckets in ms. Corresponds roughly to the
                accuracy of the timer.
        """
        self.bucket_size: int = bucket_size
        self.entries: List[_Entry[T]] = []
        self.current_tick: int = 0

    def insert(self, now: int, obj: T, then: int) -> None:
        """Inserts object into timer.

        Args:
            now: Current time in msec
            obj: Object to be inserted
            then: When to return the object strictly after.
        """
        then_key = int(then / self.bucket_size) + 1
        now_key = int(now / self.bucket_size)

        if self.entries:
            min_key = self.entries[0].end_key
            max_key = self.entries[-1].end_key

            if min_key < now_key - 10:
                # If we have ten buckets that are due and still nothing has
                # called `fetch()` then we likely have a bug that is causing a
                # memory leak.
                logger.warning(
                    "Inserting into a wheel timer that hasn't been read from recently. Item: %s",
                    obj,
                )

            if then_key <= max_key:
                # The max here is to protect against inserts for times in the past
                self.entries[max(min_key, then_key) - min_key].elements.add(obj)
                return

        next_key = now_key + 1
        if self.entries:
            last_key = self.entries[-1].end_key
        else:
            last_key = next_key

        # Handle the case when `then` is in the past and `entries` is empty.
        then_key = max(last_key, then_key)

        # Add empty entries between the end of the current list and when we want
        # to insert. This ensures there are no gaps.
        self.entries.extend(_Entry(key) for key in range(last_key, then_key + 1))

        self.entries[-1].elements.add(obj)

    def fetch(self, now: int) -> List[T]:
        """Fetch any objects that have timed out

        Args:
            now: Current time in msec

        Returns:
            List of objects that have timed out
        """
        now_key = int(now / self.bucket_size)

        ret: List[T] = []
        while self.entries and self.entries[0].end_key <= now_key:
            ret.extend(self.entries.pop(0).elements)

        return ret

    def __len__(self) -> int:
        return sum(len(entry.elements) for entry in self.entries)
