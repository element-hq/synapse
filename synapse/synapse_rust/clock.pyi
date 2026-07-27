# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

def set_virtual_time_msec(millis: int | None) -> None:
    """Pin the Rust clock to the given time, or restore the real clock with `None`.

    For tests only: Synapse's tests run against a virtual reactor clock, and
    this keeps the Rust side of the world on the same clock.
    """

def time_msec() -> int:
    """The current time as the Rust clock sees it, in milliseconds since the epoch."""
