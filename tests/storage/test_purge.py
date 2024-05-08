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

from twisted.test.proto_helpers import MemoryReactor

from synapse.api.errors import NotFoundError, SynapseError
from synapse.rest.client import room
from synapse.server import HomeServer
from synapse.util import Clock

from tests.test_utils.event_injection import inject_event
from tests.unittest import HomeserverTestCase


class PurgeTests(HomeserverTestCase):
    user_id = "@red:server"
    servlets = [room.register_servlets]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver("server")
        return hs

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.room_id = self.helper.create_room_as(self.user_id)

        self.store = hs.get_datastores().main
        self._storage_controllers = self.hs.get_storage_controllers()

    def test_purge_history(self) -> None:
        """
        Purging a room history will delete everything before the topological point.
        """
        # Send four messages to the room
        first = self.helper.send(self.room_id, body="test1")
        second = self.helper.send(self.room_id, body="test2")
        third = self.helper.send(self.room_id, body="test3")
        last = self.helper.send(self.room_id, body="test4")

        # Get the topological token
        token = self.get_success(
            self.store.get_topological_token_for_event(last["event_id"])
        )
        token_str = self.get_success(token.to_string(self.hs.get_datastores().main))

        # Purge everything before this topological token
        self.get_success(
            self._storage_controllers.purge_events.purge_history(
                self.room_id, token_str, True
            )
        )

        # 1-3 should fail and last will succeed, meaning that 1-3 are deleted
        # and last is not.
        self.get_failure(self.store.get_event(first["event_id"]), NotFoundError)
        self.get_failure(self.store.get_event(second["event_id"]), NotFoundError)
        self.get_failure(self.store.get_event(third["event_id"]), NotFoundError)
        self.get_success(self.store.get_event(last["event_id"]))

    def test_purge_history_wont_delete_extrems(self) -> None:
        """
        Purging a room history will delete everything before the topological point.
        """
        # Send four messages to the room
        first = self.helper.send(self.room_id, body="test1")
        second = self.helper.send(self.room_id, body="test2")
        third = self.helper.send(self.room_id, body="test3")
        last = self.helper.send(self.room_id, body="test4")

        # Set the topological token higher than it should be
        token = self.get_success(
            self.store.get_topological_token_for_event(last["event_id"])
        )
        assert token.topological is not None
        event = f"t{token.topological + 1}-{token.stream + 1}"

        # Purge everything before this topological token
        f = self.get_failure(
            self._storage_controllers.purge_events.purge_history(
                self.room_id, event, True
            ),
            SynapseError,
        )
        self.assertIn("greater than forward", f.value.args[0])

        # Try and get the events
        self.get_success(self.store.get_event(first["event_id"]))
        self.get_success(self.store.get_event(second["event_id"]))
        self.get_success(self.store.get_event(third["event_id"]))
        self.get_success(self.store.get_event(last["event_id"]))

    def test_purge_room(self) -> None:
        """
        Purging a room will delete everything about it.
        """
        # Send four messages to the room
        first = self.helper.send(self.room_id, body="test1")

        # Get the current room state.
        create_event = self.get_success(
            self._storage_controllers.state.get_current_state_event(
                self.room_id, "m.room.create", ""
            )
        )
        assert create_event is not None

        # Purge everything before this topological token
        self.get_success(
            self._storage_controllers.purge_events.purge_room(self.room_id)
        )

        # The events aren't found.
        self.store._invalidate_local_get_event_cache(create_event.event_id)
        self.get_failure(self.store.get_event(create_event.event_id), NotFoundError)
        self.get_failure(self.store.get_event(first["event_id"]), NotFoundError)

    def test_state_groups_state_decreases(self) -> None:
        response = self.helper.send(self.room_id, body="first")
        first_event_id = response["event_id"]

        batches = []

        previous_event_id = first_event_id
        for i in range(50):
            state_event1 = self.get_success(
                inject_event(
                    self.hs,
                    type="test.state",
                    sender=self.user_id,
                    state_key="",
                    room_id=self.room_id,
                    content={"key": i, "e": 1},
                    prev_event_ids=[previous_event_id],
                    origin_server_ts=1,
                )
            )

            state_event2 = self.get_success(
                inject_event(
                    self.hs,
                    type="test.state",
                    sender=self.user_id,
                    state_key="",
                    room_id=self.room_id,
                    content={"key": i, "e": 2},
                    prev_event_ids=[previous_event_id],
                    origin_server_ts=2,
                )
            )

            # print(state_event2.origin_server_ts - state_event1.origin_server_ts)

            message_event = self.get_success(
                inject_event(
                    self.hs,
                    type="dummy_event",
                    sender=self.user_id,
                    room_id=self.room_id,
                    content={},
                    prev_event_ids=[state_event1.event_id, state_event2.event_id],
                )
            )

            token = self.get_success(
                self.store.get_topological_token_for_event(state_event1.event_id)
            )
            batches.append(token)

            previous_event_id = message_event.event_id

        self.helper.send(self.room_id, body="last event")

        def count_state_groups() -> int:
            sql = "SELECT COUNT(*) FROM state_groups_state WHERE room_id = ?"
            rows = self.get_success(
                self.store.db_pool.execute("test_deduplicate_joins", sql, self.room_id)
            )
            return rows[0][0]

        print(count_state_groups())
        for token in batches:
            token_str = self.get_success(token.to_string(self.hs.get_datastores().main))
            self.get_success(
                self._storage_controllers.purge_events.purge_history(
                    self.room_id, token_str, False
                )
            )
            print(count_state_groups())
