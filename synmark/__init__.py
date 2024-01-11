#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

import sys
from typing import cast

from synapse.types import ISynapseReactor

try:
    from twisted.internet.epollreactor import EPollReactor as Reactor
except ImportError:
    from twisted.internet.pollreactor import PollReactor as Reactor  # type: ignore[assignment]
from twisted.internet.main import installReactor


def make_reactor() -> ISynapseReactor:
    """
    Instantiate and install a Twisted reactor suitable for testing (i.e. not the
    default global one).
    """
    reactor = Reactor()

    if "twisted.internet.reactor" in sys.modules:
        del sys.modules["twisted.internet.reactor"]
    installReactor(reactor)

    return cast(ISynapseReactor, reactor)
