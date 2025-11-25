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

    def as_millis(self) -> int:
        """Returns the duration in milliseconds."""
        return int(self / _ONE_MILLISECOND)

    def as_secs(self) -> int:
        """Returns the duration in seconds."""
        return int(self.total_seconds())
