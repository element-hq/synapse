from synapse.metrics import (
    REGISTRY,
    generate_latest,
)


def get_latest_metrics() -> dict[str, str]:
    """
    Collect the latest metrics from the registry and parse them into an easy to use map.
    The key includes the metric name and labels.

    Example output:
    {
        "synapse_util_caches_cache_size": "0.0",
        "synapse_util_caches_cache_max_size{name="some_cache",server_name="hs1"}": "777.0",
        ...
    }
    """
    metric_map = {
        x.split(b" ")[0].decode("ascii"): x.split(b" ")[1].decode("ascii")
        for x in filter(
            lambda x: len(x) > 0 and not x.startswith(b"#"),
            generate_latest(REGISTRY).split(b"\n"),
        )
    }

    return metric_map
