#!/usr/bin/env python3

#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

import argparse
import sys

from synapse.config.logger import DEFAULT_LOG_CONFIG


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-o",
        "--output-file",
        type=argparse.FileType("w"),
        default=sys.stdout,
        help="File to write the configuration to. Default: stdout",
    )

    parser.add_argument(
        "-f",
        "--log-file",
        type=str,
        default="/var/log/matrix-synapse/homeserver.log",
        help="name of the log file",
    )

    args = parser.parse_args()
    out = args.output_file
    out.write(DEFAULT_LOG_CONFIG.substitute(log_file=args.log_file))
    out.flush()


if __name__ == "__main__":
    main()
