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

from typing import Any

from synapse.synapse_rust.runtime import RustRuntime

class PythonSpanReporter:
    """Reports the Python tracer's finished spans through the homeserver's
    Rust reporter, which sends them to the Jaeger agent with Rust's own."""

    def __init__(self, runtime: RustRuntime) -> None: ...
    def report_span(self, span: Any) -> None:
        """Queue a finished jaeger-client span. Never blocks: the span is
        dropped if the queue is full or tracing is not enabled."""

def encode_python_span_for_test(
    span: Any, service_name: str, process_tags: list[tuple[str, str]]
) -> bytes:
    """The `emitBatch` message the Rust reporter would send for the finished
    jaeger-client `span`, with sequence ID 1. For tests."""

def captured_span_for_test() -> tuple[int, int, int, dict[str, str]] | None:
    """The `(trace_id, span_id, flags, baggage)` of the span a Rust task
    spawned here would take as its parent, or None. For tests."""

def span_in_blocking_task_for_test(
    runtime: RustRuntime,
) -> tuple[int, int, int, dict[str, str]] | None:
    """The parent span a task run by the `_blocking` entry points sees, in the
    same form as `captured_span_for_test`. For tests."""

def span_in_task_for_test(runtime: RustRuntime, operation: str) -> None:
    """Run a Rust span named `operation`, tagged `db.queued = 3` with a
    `commit` log, in a task with the caller's span as parent. For tests."""

def span_on_thread_for_test(operation: str) -> None:
    """Run a Rust span named `operation` on the calling thread, with the
    active Python span as parent. For tests."""

def set_tracing_enabled_for_test(enabled: bool) -> None:
    """Turn span capture on or off for the whole process. For tests."""
