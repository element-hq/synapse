#!/usr/bin/env python
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
import json
import sys
from json import JSONDecodeError

import yaml
from signedjson.key import read_signing_keys
from signedjson.sign import sign_json

from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.crypto.event_signing import add_hashes_and_signatures
from synapse.util import json_encoder


def main() -> None:
    parser = argparse.ArgumentParser(
        description="""Adds a signature to a JSON object.

Example usage:

    $ scripts-dev/sign_json.py -N test -k localhost.signing.key "{}"
    {"signatures":{"test":{"ed25519:a_ZnZh":"LmPnml6iM0iR..."}}}
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "-N",
        "--server-name",
        help="Name to give as the local homeserver. If unspecified, will be "
        "read from the config file.",
    )

    parser.add_argument(
        "-k",
        "--signing-key-path",
        help="Path to the file containing the private ed25519 key to sign the "
        "request with.",
    )

    parser.add_argument(
        "-K",
        "--signing-key",
        help="The private ed25519 key to sign the request with.",
    )

    parser.add_argument(
        "-c",
        "--config",
        default="homeserver.yaml",
        help=(
            "Path to synapse config file, from which the server name and/or signing "
            "key path will be read. Ignored if --server-name and --signing-key(-path) "
            "are both given."
        ),
    )

    parser.add_argument(
        "--sign-event-room-version",
        type=str,
        help=(
            "Sign the JSON as an event for the given room version, rather than raw JSON. "
            "This means that we will add a 'hashes' object, and redact the event before "
            "signing."
        ),
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

    parser.add_argument(
        "-o",
        "--output",
        type=argparse.FileType("w"),
        default=sys.stdout,
        help="Where to write the signed JSON. Defaults to stdout.",
    )

    args = parser.parse_args()

    if not args.server_name or not (args.signing_key_path or args.signing_key):
        read_args_from_config(args)

    if args.signing_key:
        keys = read_signing_keys([args.signing_key])
    else:
        with open(args.signing_key_path) as f:
            keys = read_signing_keys(f)

    json_to_sign = args.input_data
    if json_to_sign is None:
        json_to_sign = args.input.read()

    try:
        obj = json.loads(json_to_sign)
    except JSONDecodeError as e:
        print("Unable to parse input as JSON: %s" % e, file=sys.stderr)
        sys.exit(1)

    if not isinstance(obj, dict):
        print("Input json was not an object", file=sys.stderr)
        sys.exit(1)

    if args.sign_event_room_version:
        room_version = KNOWN_ROOM_VERSIONS.get(args.sign_event_room_version)
        if not room_version:
            print(
                f"Unknown room version {args.sign_event_room_version}", file=sys.stderr
            )
            sys.exit(1)
        add_hashes_and_signatures(room_version, obj, args.server_name, keys[0])
    else:
        sign_json(obj, args.server_name, keys[0])

    for c in json_encoder.iterencode(obj):
        args.output.write(c)
    args.output.write("\n")


def read_args_from_config(args: argparse.Namespace) -> None:
    with open(args.config) as fh:
        config = yaml.safe_load(fh)
        if not args.server_name:
            args.server_name = config["server_name"]
        if not args.signing_key_path and not args.signing_key:
            if "signing_key" in config:
                args.signing_key = config["signing_key"]
            elif "signing_key_path" in config:
                args.signing_key_path = config["signing_key_path"]
            else:
                print(
                    "A signing key must be given on the commandline or in the config file.",
                    file=sys.stderr,
                )
                sys.exit(1)


if __name__ == "__main__":
    main()
