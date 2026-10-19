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

from synapse.synapse_rust.runtime import RustRuntime

def captured_span_for_test() -> tuple[int, int, int, dict[str, str]] | None:
    """The `(trace_id, span_id, flags, baggage)` of the span a Rust task
    spawned here would take as its parent, or None. For tests."""

def span_in_blocking_task_for_test(
    runtime: RustRuntime,
) -> tuple[int, int, int, dict[str, str]] | None:
    """The parent span a task run by the `_blocking` entry points sees, in the
    same form as `captured_span_for_test`. For tests."""

def set_tracing_enabled_for_test(enabled: bool) -> None:
    """Turn span capture on or off for the whole process. For tests."""
