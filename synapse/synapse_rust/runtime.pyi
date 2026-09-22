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
