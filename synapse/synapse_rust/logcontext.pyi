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

from typing import Optional

class ContextResourceUsage:
    """Tracks the resources used by a log context."""

    ru_stime: float
    ru_utime: float
    db_txn_count: int
    db_txn_duration_sec: float
    db_sched_duration_sec: float
    evt_db_fetch_count: int

    def __init__(self, copy_from: "Optional[ContextResourceUsage]" = None) -> None: ...
    def copy(self) -> "ContextResourceUsage": ...
    def reset(self) -> None: ...
    def __iadd__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
    def __isub__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
    def __add__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
    def __sub__(self, other: "ContextResourceUsage") -> "ContextResourceUsage": ...
