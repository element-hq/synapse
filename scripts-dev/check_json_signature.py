#!/usr/bin/env python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
import argparse
import json
import sys
from json import JSONDecodeError

import nacl.signing
from canonicaljson import encode_canonical_json
from unpaddedbase64 import decode_base64


def main() -> None:
    parser = argparse.ArgumentParser(
        description="""Checks the signature on a JSON object.

Example usage:

    $ cat input.json | scripts-dev/check_json_signature.py -K "base64signingkey" -s "base64signature"
    Error: signature does not match

See also sign_json.py.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-K",
        "--verify-key",
        help="The public part of the key the json was signed with",
        required=True,
    )

    parser.add_argument(
        "-s",
        "--signature",
        help="The signature to check",
        required=True,
    )

    input_args = parser.add_mutually_exclusive_group()

    input_args.add_argument("input_data", nargs="?", help="Raw JSON to be signed.")

    input_args.add_argument(
        "-i",
        "--input",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help=(
            "A file from which to read the JSON to be signed. If neither --input nor "
            "input_data are given, JSON will be read from stdin."
        ),
    )

    args = parser.parse_args()

    try:
        key_bytes = decode_base64(args.verify_key)
        verify_key = nacl.signing.VerifyKey(key_bytes)
    except Exception as e:
        print("Invalid verify key: %s" % e, file=sys.stderr)
        sys.exit(1)

    try:
        signature = decode_base64(args.signature)
    except Exception:
        print("Invalid signature base64", file=sys.stderr)
        sys.exit(1)

    json_to_check = args.input_data
    if json_to_check is None:
        json_to_check = args.input.read()

    try:
        obj = json.loads(json_to_check)
    except JSONDecodeError as e:
        print("Unable to parse input as JSON: %s" % e, file=sys.stderr)
        sys.exit(1)

    if not isinstance(obj, dict):
        print("Input json was not an object", file=sys.stderr)
        sys.exit(1)

    obj.pop("signatures", None)
    obj.pop("unsigned", None)
    canonical = encode_canonical_json(obj)

    try:
        verify_key.verify(canonical, signature)
    except Exception as e:
        print("Error: signature does not match: %s" % e)
        sys.exit(1)

    print("ok")


if __name__ == "__main__":
    main()
