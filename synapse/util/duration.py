#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from datetime import timedelta
from typing import overload

# Constant so we don't keep creating new timedelta objects when calling
# `.as_millis()`.
_ONE_MILLISECOND = timedelta(milliseconds=1)


class Duration(timedelta):
    """A subclass of timedelta that adds a convenience method for getting
    the duration in milliseconds.

    Examples:

    ```
    duration = Duration(hours=2)
    print(duration.as_millis())  # Outputs: 7200000
    ```
    """

    # Using `__new__` (instead of `__init__`) because that's what `timedelta` uses
    def __new__(
        cls,
        # The whole goal of overriding `__new__` is to require keyword-only arguments.
        # Without this, `Duration(5)` would create a duration represnting 5 *days*
        # (timedelta's default), but callers almost certainly want to specify which unit
        # like seconds or hours.
        *,
        days: float = 0,
        seconds: float = 0,
        microseconds: float = 0,
        milliseconds: float = 0,
        minutes: float = 0,
        hours: float = 0,
        weeks: float = 0,
    ) -> "Duration":
        return super().__new__(
            cls,
            days=days,
            seconds=seconds,
            microseconds=microseconds,
            milliseconds=milliseconds,
            minutes=minutes,
            hours=hours,
            weeks=weeks,
        )

    def as_millis(self) -> int:
        """Returns the duration in milliseconds."""
        return int(self / _ONE_MILLISECOND)

    def as_secs(self) -> float:
        """Returns the duration in seconds."""
        return self.total_seconds()

    # Override arithmetic operations to return Duration instances

    def __add__(self, other: timedelta) -> "Duration":
        """Add two durations together, returning a Duration."""
        result = super().__add__(other)
        return Duration(seconds=result.total_seconds())

    def __radd__(self, other: timedelta) -> "Duration":
        """Add two durations together (reversed), returning a Duration."""
        result = super().__radd__(other)
        return Duration(seconds=result.total_seconds())

    def __sub__(self, other: timedelta) -> "Duration":
        """Subtract two durations, returning a Duration."""
        result = super().__sub__(other)
        return Duration(seconds=result.total_seconds())

    def __rsub__(self, other: timedelta) -> "Duration":
        """Subtract two durations (reversed), returning a Duration."""
        result = super().__rsub__(other)
        return Duration(seconds=result.total_seconds())

    def __mul__(self, other: float) -> "Duration":
        """Multiply a duration by a scalar, returning a Duration."""
        result = super().__mul__(other)
        return Duration(seconds=result.total_seconds())

    def __rmul__(self, other: float) -> "Duration":
        """Multiply a duration by a scalar (reversed), returning a Duration."""
        result = super().__rmul__(other)
        return Duration(seconds=result.total_seconds())

    @overload
    def __truediv__(self, other: timedelta) -> float: ...

    @overload
    def __truediv__(self, other: float) -> "Duration": ...

    def __truediv__(self, other: float | timedelta) -> "Duration | float":
        """Divide a duration by a scalar or another duration.

        If dividing by a scalar, returns a Duration.
        If dividing by a timedelta, returns a float ratio.
        """
        result = super().__truediv__(other)
        if isinstance(other, timedelta):
            # Dividing by a timedelta gives a float ratio
            assert isinstance(result, float)
            return result
        else:
            # Dividing by a scalar gives a Duration
            assert isinstance(result, timedelta)
            return Duration(seconds=result.total_seconds())

    @overload
    def __floordiv__(self, other: timedelta) -> int: ...

    @overload
    def __floordiv__(self, other: int) -> "Duration": ...

    def __floordiv__(self, other: int | timedelta) -> "Duration | int":
        """Floor divide a duration by a scalar or another duration.

        If dividing by a scalar, returns a Duration.
        If dividing by a timedelta, returns an int ratio.
        """
        result = super().__floordiv__(other)
        if isinstance(other, timedelta):
            # Dividing by a timedelta gives an int ratio
            assert isinstance(result, int)
            return result
        else:
            # Dividing by a scalar gives a Duration
            assert isinstance(result, timedelta)
            return Duration(seconds=result.total_seconds())
