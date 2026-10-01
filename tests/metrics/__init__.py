#
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
