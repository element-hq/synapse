from synapse.server import HomeServer

class RustRuntime:
    """The per-homeserver state for the Rust side of Synapse.

    Holds the tokio thread pool (started lazily on first use, shut down with the
    Homeserver). Rust classes that need them take this as a constructor
    argument. Get it from `hs.get_rust_runtime()`.
    """

    def __init__(
        self,
        hs: HomeServer,
        worker_threads: int = 4,
    ) -> None: ...
    def time_msec(self) -> int:
        """Test utility only. Normal code should use
        `hs.get_clock().time_msec()`.

        The current time in milliseconds since the Unix epoch, as the Rust side
        sees it. This and `Clock.time_msec()` agree because both read the system
        clock, `clock_gettime(CLOCK_REALTIME)` on Linux.
        """

    def expire_caches(self, expiry_secs: float) -> int:
        """Evict entries not read for `expiry_secs` from every Rust cache of
        this homeserver, and return how many were evicted.

        Called from `synapse.util.caches.lrucache._expire_old_entries`, so that
        Rust and Python caches expire off one tick and one set of config
        options."""

    def evict_coldest(self, before_secs: float, max: int) -> tuple[int, float | None]:
        """Evict up to `max` entries last read before `before_secs` (seconds
        since the Unix epoch), coldest first across every Rust cache of this
        homeserver.

        Returns how many were evicted, and when the coldest entry left was last
        read, or None if the Rust caches are empty. Nothing in them gets any
        colder than that, so a caller can skip calling again until it wants to
        evict entries read after it."""

    def enable_tracing(
        self,
        service_name: str,
        process_tags: list[tuple[str, str]],
        reporting_host: str,
        reporting_port: int,
        sampler_type: str,
        sampler_param: float,
    ) -> None:
        """Report spans from Rust to the Jaeger agent, as the same service as
        the Python tracer. Called by `init_tracer`.

        `sampler_type` is `const` or `probabilistic`, with `sampler_param` the
        decision or rate. Any other type means Rust never starts a sampled
        trace of its own. The agent's host name is resolved when spans are
        sent. Raises `OSError` only if the reporter thread cannot start. A
        second call is ignored."""

    def tracing_config_for_test(
        self,
    ) -> tuple[str, list[tuple[str, str]], str, int, str] | None:
        """The settings `enable_tracing` stored, as `(service_name,
        process_tags, reporting_host, reporting_port, sampler)`, or None. For
        tests."""

    def tracing_stats(self) -> tuple[int, int, int, int, int, int, int] | None:
        """The Jaeger reporter's counters, as `(queue_depth, spans_sent,
        batches_sent, dropped_queue_full, dropped_oversize, dropped_closed,
        send_errors)`, or None if tracing is not enabled."""

    def report_span_for_test(
        self,
        operation: str,
        trace_id: int,
        span_id: int,
        parent_id: int,
        flags: int,
        tags: list[tuple[str, str]],
    ) -> None:
        """Report a span started now and one millisecond long. For tests."""

    def flush_tracing_for_test(self, timeout_secs: float) -> bool:
        """Send the queued spans now. Returns whether they went within the
        timeout. For tests."""

    def shutdown_tracing_for_test(self) -> None:
        """Shut the Jaeger reporter down as homeserver shutdown does. For
        tests."""

def set_virtual_time_msec(millis: int) -> None:
    """Pin the Rust clock to the given time. Only for tests, which run
    against a virtual reactor clock."""
