#!/usr/bin/env python
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
import argparse
import os
import sys

from signedjson.key import generate_signing_key, write_signing_keys

from synapse.util.stringutils import random_string


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "-o",
        "--output_file",
        type=str,
        default="-",
        help="Where to write the output to",
    )
    args = parser.parse_args()

    key_id = "a_" + random_string(4)
    key = (generate_signing_key(key_id),)
    if args.output_file == "-":
        write_signing_keys(sys.stdout, key)
    else:
        with open(
            args.output_file, "w", opener=lambda p, f: os.open(p, f, mode=0o640)
        ) as signing_key_file:
            write_signing_keys(signing_key_file, key)


if __name__ == "__main__":
    main()
