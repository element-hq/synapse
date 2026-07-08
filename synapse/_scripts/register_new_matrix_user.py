#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021-22 The Matrix.org Foundation C.I.C.
# Copyright 2015, 2016 OpenMarket Ltd
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
import getpass
import hashlib
import hmac
import logging
import sys
from typing import Any, Callable, Iterable, TextIO

import requests
import yaml
from typing_extensions import Never

_CONFLICTING_SHARED_SECRET_OPTS_ERROR = """\
Conflicting options 'registration_shared_secret' and 'registration_shared_secret_path'
are both defined in config file.
"""

_NO_SHARED_SECRET_OPTS_ERROR = """\
No 'registration_shared_secret' or 'registration_shared_secret_path' defined in config.
"""

_EMPTY_SHARED_SECRET_PATH_OPTS_ERROR = """\
The secret given via `registration_shared_secret_path` must not be empty.
"""

_DEFAULT_SERVER_URL = "http://localhost:8008"


def request_registration(
    user: str,
    password: str,
    server_location: str,
    shared_secret: str,
    admin: bool = False,
    user_type: str | None = None,
    _print: Callable[[str], None] = print,
    exit: Callable[[int], None] = sys.exit,
    exists_ok: bool = False,
) -> None:
    url = "%s/_synapse/admin/v1/register" % (server_location.rstrip("/"),)

    # Get the nonce
    r = requests.get(url)

    if r.status_code != 200:
        _print("ERROR! Received %d %s" % (r.status_code, r.reason))
        if 400 <= r.status_code < 500:
            try:
                _print(r.json()["error"])
            except Exception:
                pass
        return exit(1)

    nonce = r.json()["nonce"]

    mac = hmac.new(key=shared_secret.encode("utf8"), digestmod=hashlib.sha1)

    mac.update(nonce.encode("utf8"))
    mac.update(b"\x00")
    mac.update(user.encode("utf8"))
    mac.update(b"\x00")
    mac.update(password.encode("utf8"))
    mac.update(b"\x00")
    mac.update(b"admin" if admin else b"notadmin")
    if user_type:
        mac.update(b"\x00")
        mac.update(user_type.encode("utf8"))

    hex_mac = mac.hexdigest()

    data = {
        "nonce": nonce,
        "username": user,
        "password": password,
        "mac": hex_mac,
        "admin": admin,
        "user_type": user_type,
    }

    _print("Sending registration request...")
    r = requests.post(url, json=data)

    if r.status_code != 200:
        response = r.json()
        if exists_ok and response["errcode"] == "M_USER_IN_USE":
            _print("User already exists. Skipping.")
            return
        _print("ERROR! Received %d %s" % (r.status_code, r.reason))
        if 400 <= r.status_code < 500:
            try:
                _print(r.json()["error"])
            except Exception:
                pass
        return exit(1)

    _print("Success!")


def register_new_user(
    user: str,
    password: str,
    server_location: str,
    shared_secret: str,
    admin: bool | None,
    user_type: str | None,
    exists_ok: bool = False,
) -> None:
    if not user:
        try:
            default_user: str | None = getpass.getuser()
        except Exception:
            default_user = None

        if default_user:
            user = input("New user localpart [%s]: " % (default_user,))
            if not user:
                user = default_user
        else:
            user = input("New user localpart: ")

    if not user:
        print("Invalid user name")
        sys.exit(1)

    if not password:
        password = getpass.getpass("Password: ")

        if not password:
            print("Password cannot be blank.")
            sys.exit(1)

        confirm_password = getpass.getpass("Confirm password: ")

        if password != confirm_password:
            print("Passwords do not match")
            sys.exit(1)

    if admin is None:
        admin_inp = input("Make admin [no]: ")
        if admin_inp in ("y", "yes", "true"):
            admin = True
        else:
            admin = False

    request_registration(
        user,
        password,
        server_location,
        shared_secret,
        bool(admin),
        user_type,
        exists_ok=exists_ok,
    )


def bail(err_msg: str) -> Never:
    """Prints the given message to stderr and exits."""
    print(err_msg, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    logging.captureWarnings(True)

    parser = argparse.ArgumentParser(
        description="Used to register new users with a given homeserver when"
        " registration has been disabled. The homeserver must be"
        " configured with the 'registration_shared_secret' option"
        " set."
    )
    parser.add_argument(
        "-u",
        "--user",
        default=None,
        help="Local part of the new user. Will prompt if omitted.",
    )
    parser.add_argument(
        "--exists-ok",
        action="store_true",
        help="Do not fail if user already exists.",
    )
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument(
        "-p",
        "--password",
        default=None,
        help="New password for user. Will prompt for a password if "
        "this flag and `--password-file` are both omitted.",
    )
    password_group.add_argument(
        "--password-file",
        default=None,
        help="File containing the new password for user. If set, will override `--password`.",
    )
    parser.add_argument(
        "-t",
        "--user_type",
        default=None,
        help="User type as specified in synapse.api.constants.UserTypes",
    )

    admin_group = parser.add_mutually_exclusive_group()
    admin_group.add_argument(
        "-a",
        "--admin",
        action="store_true",
        help=(
            "Register new user as an admin. "
            "Will prompt if --no-admin is not set either."
        ),
    )
    admin_group.add_argument(
        "--no-admin",
        action="store_true",
        help=(
            "Register new user as a regular user. "
            "Will prompt if --admin is not set either."
        ),
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-c",
        "--config",
        action="append",
        type=argparse.FileType("r"),
        help="Path to server config file. Used to read in shared secret.",
    )

    group.add_argument(
        "-k", "--shared-secret", help="Shared secret as defined in server config file."
    )

    parser.add_argument(
        "server_url",
        nargs="?",
        help="URL to use to talk to the homeserver. By default, tries to find a "
        "suitable URL from the configuration file. Otherwise, defaults to "
        f"'{_DEFAULT_SERVER_URL}'.",
    )

    args = parser.parse_args()

    config: dict[str, Any] | None = None
    if "config" in args and args.config:
        config = _read_config_files(args.config)

    if args.shared_secret:
        secret = args.shared_secret
    else:
        # argparse should check that we have either config or shared secret
        assert config is not None

        secret = config.get("registration_shared_secret")
        if not isinstance(secret, (str, type(None))):
            bail("registration_shared_secret is not a string.")
        secret_file = config.get("registration_shared_secret_path")
        if not isinstance(secret_file, (str, type(None))):
            bail("registration_shared_secret_path is not a string.")

        if not secret and not secret_file:
            bail(_NO_SHARED_SECRET_OPTS_ERROR)
        elif secret and secret_file:
            bail(_CONFLICTING_SHARED_SECRET_OPTS_ERROR)
        elif not secret and secret_file:
            secret = _read_file(secret_file, "registration_shared_secret_path").strip()
            if not secret:
                bail(_EMPTY_SHARED_SECRET_PATH_OPTS_ERROR)

    if args.password_file:
        password = _read_file(args.password_file, "password-file").strip()
    else:
        password = args.password

    if args.server_url:
        server_url = args.server_url
    elif config is not None:
        server_url = _find_client_listener(config)
        if not server_url:
            server_url = _DEFAULT_SERVER_URL
            print(
                "Unable to find a suitable HTTP listener in the configuration file. "
                f"Trying {server_url} as a last resort.",
                file=sys.stderr,
            )
    else:
        server_url = _DEFAULT_SERVER_URL
        print(
            f"No server url or configuration file given. Defaulting to {server_url}.",
            file=sys.stderr,
        )

    admin = None
    if args.admin or args.no_admin:
        admin = args.admin

    register_new_user(
        args.user,
        password,
        server_url,
        secret,
        admin,
        args.user_type,
        exists_ok=args.exists_ok,
    )


# Adapted from synapse.config._base.
def _read_config_files(config_files: Iterable[TextIO]) -> dict[str, Any]:
    """Read the config files and shallowly merge them into a dict.

    Successive configurations are shallowly merged into ones provided earlier,
    i.e., entirely replacing top-level sections of the configuration.

    Args:
        config_files: A list of the config files to read

    Returns:
        The configuration dictionary.
    """
    specified_config = {}
    for config_file in config_files:
        yaml_config = yaml.safe_load(config_file)

        if not isinstance(yaml_config, dict):
            err = "File %r is empty or doesn't parse into a key-value map. IGNORING."
            print(err % (config_file,))
            continue

        specified_config.update(yaml_config)

    return specified_config


def _read_file(file_path: Any, config_path: str) -> str:
    """Check the given file exists, and read it into a string

    If it does not, exit with an error indicating the problem

    Args:
        file_path: the file to be read
        config_path: where in the configuration file_path came from, so that a useful
           error can be emitted if it does not exist.
    Returns:
        content of the file.
    """
    if not isinstance(file_path, str):
        print(f"{config_path} setting is not a string", file=sys.stderr)
        sys.exit(1)

    try:
        with open(file_path) as file_stream:
            return file_stream.read()
    except OSError as e:
        print(f"Error accessing file {file_path}: {e}", file=sys.stderr)
        sys.exit(1)


def _find_client_listener(config: dict[str, Any]) -> str | None:
    # try to find a listener in the config. Returns a host:port pair
    for listener in config.get("listeners", []):
        if listener.get("type") != "http" or listener.get("tls", False):
            continue

        if not any(
            name == "client"
            for resource in listener.get("resources", [])
            for name in resource.get("names", [])
        ):
            continue

        # TODO: consider bind_addresses
        return f"http://localhost:{listener['port']}"

    # no suitable listeners?
    return None


if __name__ == "__main__":
    main()
