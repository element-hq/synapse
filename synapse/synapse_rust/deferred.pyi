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

from twisted.internet.defer import Deferred

from synapse.synapse_rust.runtime import RustRuntime

def sleep_for_test(
    runtime: RustRuntime, seconds: float, abort_on_cancel: bool
) -> "Deferred[str]":
    """A Deferred that fires with "slept" after `seconds`. With
    `abort_on_cancel`, cancelling it aborts the tokio task behind it. For
    tests of Rust deferreds."""

def dropped_sleeps_for_test() -> int:
    """How many `sleep_for_test` futures have been dropped so far, whether
    they ran to the end or were aborted."""
