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
import logging
from typing import cast

import yaml

from twisted.internet import defer, reactor as reactor_

from synapse.config.homeserver import HomeServerConfig
from synapse.server import HomeServer
from synapse.storage import DataStore
from synapse.types import ISynapseReactor

# Cast safety: Twisted does some naughty magic which replaces the
# twisted.internet.reactor module with a Reactor instance at runtime.
reactor = cast(ISynapseReactor, reactor_)
logger = logging.getLogger("update_database")


class MockHomeserver(HomeServer):
    DATASTORE_CLASS = DataStore

    def __init__(self, config: HomeServerConfig):
        super().__init__(
            hostname=config.server.server_name,
            config=config,
            reactor=reactor,
        )


def run_background_updates(hs: HomeServer) -> None:
    main = hs.get_datastores().main
    state = hs.get_datastores().state

    async def run_background_updates() -> None:
        await main.db_pool.updates.run_background_updates(sleep=False)
        if state:
            await state.db_pool.updates.run_background_updates(sleep=False)
        # Stop the reactor to exit the script once every background update is run.
        reactor.stop()

    def run() -> None:
        # Apply all background updates on the database.
        defer.ensureDeferred(
            hs.run_as_background_process(
                "background_updates",
                run_background_updates,
            )
        )

    hs.get_clock().call_when_running(run)

    reactor.run()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Updates a synapse database to the latest schema and optionally runs background updates"
            " on it."
        )
    )
    parser.add_argument("-v", action="store_true")
    parser.add_argument(
        "--database-config",
        type=argparse.FileType("r"),
        required=True,
        help="Synapse configuration file, giving the details of the database to be updated",
    )
    parser.add_argument(
        "--run-background-updates",
        action="store_true",
        required=False,
        help="run background updates after upgrading the database schema",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.v else logging.INFO,
        format="%(asctime)s - %(name)s - %(lineno)d - %(levelname)s - %(message)s",
    )

    # Load, process and sanity-check the config.
    hs_config = yaml.safe_load(args.database_config)

    config = HomeServerConfig()
    config.parse_config_dict(hs_config, "", "")

    # Instantiate and initialise the homeserver object.
    hs = MockHomeserver(config)

    # Setup instantiates the store within the homeserver object and updates the
    # DB.
    hs.setup()

    # This will cause all of the relevant storage classes to be instantiated and call
    # `register_background_update_handler(...)`,
    # `register_background_index_update(...)`,
    # `register_background_validate_constraint(...)`, etc so they are available to use
    # if we are asked to run those background updates.
    hs.get_storage_controllers()

    if args.run_background_updates:
        run_background_updates(hs)


if __name__ == "__main__":
    main()
