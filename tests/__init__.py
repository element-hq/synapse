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

# Set up an asyncio event loop for tests.
import asyncio as _asyncio
import nest_asyncio as _nest_asyncio

_test_asyncio_loop = _asyncio.new_event_loop()
_asyncio.set_event_loop(_test_asyncio_loop)

# Allow nested event loop calls (run_until_complete inside run_until_complete)
_nest_asyncio.apply(_test_asyncio_loop)
