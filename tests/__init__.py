#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

# Install the asyncio reactor BEFORE any other Twisted imports.
# This ensures asyncio.get_running_loop() works inside Twisted-driven code,
# enabling native asyncio primitives (Future, Event, create_task, etc.)
import asyncio as _asyncio

try:
    from twisted.internet import asyncioreactor

    _test_asyncio_loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(_test_asyncio_loop)
    asyncioreactor.install(_test_asyncio_loop)
except Exception:
    pass  # Already installed or Twisted not available
