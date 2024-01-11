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
import sys
import time
from typing import NoReturn, Optional

from signedjson.key import encode_verify_key_base64, get_verify_key, read_signing_keys
from signedjson.types import VerifyKey


def exit(status: int = 0, message: Optional[str] = None) -> NoReturn:
    if message:
        print(message, file=sys.stderr)
    sys.exit(status)


def format_plain(public_key: VerifyKey) -> None:
    print(
        "%s:%s %s"
        % (
            public_key.alg,
            public_key.version,
            encode_verify_key_base64(public_key),
        )
    )


def format_for_config(public_key: VerifyKey, expiry_ts: int) -> None:
    print(
        '  "%s:%s": { key: "%s", expired_ts: %i }'
        % (
            public_key.alg,
            public_key.version,
            encode_verify_key_base64(public_key),
            expiry_ts,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "key_file",
        nargs="+",
        type=argparse.FileType("r"),
        help="The key file to read",
    )

    parser.add_argument(
        "-x",
        action="store_true",
        dest="for_config",
        help="format the output for inclusion in the old_signing_keys config setting",
    )

    parser.add_argument(
        "--expiry-ts",
        type=int,
        default=int(time.time() * 1000) + 6 * 3600000,
        help=(
            "The expiry time to use for -x, in milliseconds since 1970. The default "
            "is (now+6h)."
        ),
    )

    args = parser.parse_args()

    formatter = (
        (lambda k: format_for_config(k, args.expiry_ts))
        if args.for_config
        else format_plain
    )

    for file in args.key_file:
        try:
            res = read_signing_keys(file)
        except Exception as e:
            exit(
                status=1,
                message="Error reading key from file %s: %s %s"
                % (file.name, type(e), e),
            )
        for key in res:
            formatter(get_verify_key(key))


if __name__ == "__main__":
    main()
