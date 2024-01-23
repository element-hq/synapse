#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 Beeper
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


"""
Forces through the `current_state_events_membership` background job so checks
for its completion can be removed.

Note the background job must still remain defined in the database class.
"""
from synapse.config.homeserver import HomeServerConfig
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import BaseDatabaseEngine


def run_upgrade(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
    config: HomeServerConfig,
) -> None:
    cur.execute("SELECT update_name FROM background_updates")
    rows = cur.fetchall()
    for row in rows:
        if row[0] == "current_state_events_membership":
            break
    # No pending background job so nothing to do here
    else:
        return

    # Populate membership field for all current_state_events, this may take
    # a while but was originally handled via a background update in 2019.
    cur.execute(
        """
        UPDATE current_state_events
        SET membership = (
            SELECT membership FROM room_memberships
            WHERE event_id = current_state_events.event_id
        )
        """
    )

    # Finally, delete the background job because we've handled it above
    cur.execute(
        """
        DELETE FROM background_updates
        WHERE update_name = 'current_state_events_membership'
        """
    )
