from synapse.types import ISynapseReactor

class RustRuntime:
    """The per-homeserver state for the Rust side of Synapse.

    Holds the tokio thread pool (started lazily on first use, shut down by a
    reactor shutdown trigger) and a handle to the reactor. Rust classes that
    need either take this as a constructor argument; get it from
    `hs.get_rust_runtime()`.
    """

    def __init__(
        self,
        reactor: ISynapseReactor,
        worker_threads: int = 4,
    ) -> None: ...
