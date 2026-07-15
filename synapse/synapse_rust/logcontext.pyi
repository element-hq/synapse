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

from synapse.logging.context import LoggingContextOrSentinel

def current_context() -> LoggingContextOrSentinel:
    """Get the current logging context.

    Resolves the tokio task-local first (so logging emitted while a Rust task is
    being polled is attributed to the context that was current when the task was
    spawned), then this OS thread's slot, then the sentinel.
    """

def swap_current_context(
    context: LoggingContextOrSentinel,
) -> LoggingContextOrSentinel:
    """Set this OS thread's current logging context, returning the one that was
    previously current on this thread.

    This is the raw slot write only: no resource-usage accounting or
    thread-affinity checks. `synapse.logging.context.set_current_context` wraps
    this with the `getrusage` start/stop bookkeeping.
    """

def register_sentinel(sentinel: LoggingContextOrSentinel) -> None:
    """Register the Python sentinel logcontext with the Rust slot.

    Called once from `synapse.logging.context` at import time so that the object
    returned by `current_context()` when no context is set is Python's
    `SENTINEL_CONTEXT` singleton (preserving identity and `bool()` semantics).
    """
