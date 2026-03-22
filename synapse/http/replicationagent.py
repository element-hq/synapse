#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import logging
from typing import Any

from synapse.config.workers import (
    InstanceLocationConfig,
)

logger = logging.getLogger(__name__)


# The Twisted-based ReplicationEndpointFactory and ReplicationAgent classes have been
# removed as part of the Twisted->asyncio migration. Replication HTTP requests are now
# handled by NativeReplicationClient using aiohttp.
#
# For backward compatibility of imports, we provide stubs.


class ReplicationEndpointFactory:
    """Stub: The Twisted ReplicationEndpointFactory has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ReplicationEndpointFactory has been removed. "
            "Use NativeReplicationClient instead."
        )


class ReplicationAgent:
    """Stub: The Twisted ReplicationAgent has been removed."""
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ReplicationAgent has been removed. "
            "Use NativeReplicationClient instead."
        )
