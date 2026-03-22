#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

"""
Backward-compatibility shim.

The canonical implementations of ``SynapseRequest``, ``SynapseSite``, and
``RequestInfo`` now live in ``synapse.http.aiohttp_shim``.  This module
re-exports them so that the many existing ``from synapse.http.site import …``
statements throughout the codebase continue to work.
"""

from synapse.http.aiohttp_shim import (  # noqa: F401
    RequestInfo,
    SynapseRequest,
    SynapseSite,
)

__all__ = [
    "SynapseRequest",
    "SynapseSite",
    "RequestInfo",
]
