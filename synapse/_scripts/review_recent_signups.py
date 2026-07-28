#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from datetime import datetime

import attr

from synapse.config._base import (
    Config,
    ConfigError,
    RootConfig,
    find_config_files,
    read_config_files,
)
from synapse.config.database import DatabaseConfig
from synapse.config.server import ServerConfig
from synapse.storage.database import DatabasePool, LoggingTransaction, make_conn
from synapse.storage.engines import create_engine


class ReviewConfig(RootConfig):
    "A config class that just pulls out the server and database config"

    config_classes = [ServerConfig, DatabaseConfig]


@attr.s(auto_attribs=True)
class UserInfo:
    user_id: str
    creation_ts: int
    emails: list[str] = attr.Factory(list)
    private_rooms: list[str] = attr.Factory(list)
    public_rooms: list[str] = attr.Factory(list)
    ips: list[str] = attr.Factory(list)


def get_recent_users(
    txn: LoggingTransaction, since_ms: int, exclude_app_service: bool
) -> list[UserInfo]:
    """Fetches recently registered users and some info on them."""

    sql = """
        SELECT name, creation_ts FROM users
        WHERE
            ? <= creation_ts
            AND deactivated = 0
    """

    if exclude_app_service:
        sql += " AND appservice_id IS NULL"

    txn.execute(sql, (since_ms / 1000,))

    user_infos = [UserInfo(user_id, creation_ts) for user_id, creation_ts in txn]

    for user_info in user_infos:
        user_info.emails = DatabasePool.simple_select_onecol_txn(
            txn,
            table="user_threepids",
            keyvalues={"user_id": user_info.user_id, "medium": "email"},
            retcol="address",
        )

        sql = """
            SELECT room_id, canonical_alias, name, join_rules
            FROM local_current_membership
            INNER JOIN room_stats_state USING (room_id)
            WHERE user_id = ? AND membership = 'join'
        """

        txn.execute(sql, (user_info.user_id,))
        for room_id, canonical_alias, name, join_rules in txn:
            if join_rules == "public":
                user_info.public_rooms.append(canonical_alias or name or room_id)
            else:
                user_info.private_rooms.append(canonical_alias or name or room_id)

        user_info.ips = DatabasePool.simple_select_onecol_txn(
            txn,
            table="user_ips",
            keyvalues={"user_id": user_info.user_id},
            retcol="ip",
        )

    return user_infos


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config-path",
        action="append",
        metavar="CONFIG_FILE",
        help="The config files for Synapse.",
        required=True,
    )
    parser.add_argument(
        "-s",
        "--since",
        metavar="duration",
        help="Specify how far back to review user registrations for, defaults to 7d (i.e. 7 days).",
        default="7d",
    )
    parser.add_argument(
        "-e",
        "--exclude-emails",
        action="store_true",
        help="Exclude users that have validated email addresses.",
    )
    parser.add_argument(
        "-u",
        "--only-users",
        action="store_true",
        help="Only print user IDs that match.",
    )
    parser.add_argument(
        "-a",
        "--exclude-app-service",
        help="Exclude appservice users.",
        action="store_true",
    )

    config = ReviewConfig()

    config_args = parser.parse_args(sys.argv[1:])
    config_files = find_config_files(search_paths=config_args.config_path)
    config_dict = read_config_files(config_files)
    config.parse_config_dict(config_dict, "", "")

    server_name = config.server.server_name
    if not isinstance(server_name, str):
        raise ConfigError("Must be a string", ("server_name",))

    since_ms = time.time() * 1000 - Config.parse_duration(config_args.since)
    exclude_users_with_email = config_args.exclude_emails
    exclude_users_with_appservice = config_args.exclude_app_service
    include_context = not config_args.only_users

    for database_config in config.database.databases:
        if "main" in database_config.databases:
            break

    engine = create_engine(database_config.config)

    with make_conn(
        db_config=database_config,
        engine=engine,
        default_txn_name="review_recent_signups",
        server_name=server_name,
    ) as db_conn:
        # This generates a type of Cursor, not LoggingTransaction.
        user_infos = get_recent_users(
            db_conn.cursor(),
            since_ms,  # type: ignore[arg-type]
            exclude_users_with_appservice,
        )

    for user_info in user_infos:
        if exclude_users_with_email and user_info.emails:
            continue

        if include_context:
            print_public_rooms = ""
            if user_info.public_rooms:
                print_public_rooms = "(" + ", ".join(user_info.public_rooms[:3])

                if len(user_info.public_rooms) > 3:
                    print_public_rooms += ", ..."

                print_public_rooms += ")"

            print("# Created:", datetime.fromtimestamp(user_info.creation_ts))
            print("# Email:", ", ".join(user_info.emails) or "None")
            print("# IPs:", ", ".join(user_info.ips))
            print(
                "# Number joined public rooms:",
                len(user_info.public_rooms),
                print_public_rooms,
            )
            print("# Number joined private rooms:", len(user_info.private_rooms))
            print("#")

        print(user_info.user_id)

        if include_context:
            print()


if __name__ == "__main__":
    main()
