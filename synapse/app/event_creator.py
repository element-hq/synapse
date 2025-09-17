#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from synapse.app.generic_worker import load_config, start
from synapse.util.logcontext import LoggingContext


def main() -> None:
    homeserver_config = load_config(sys.argv[1:])
    with LoggingContext(name="main"):
        start(homeserver_config)


if __name__ == "__main__":
    main()
