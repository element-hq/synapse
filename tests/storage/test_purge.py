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

from twisted.internet.testing import MemoryReactor

from synapse.api.errors import NotFoundError, SynapseError
from synapse.rest.client import room
from synapse.server import HomeServer
from synapse.types.state import StateFilter
from synapse.types.storage import _BackgroundUpdates
from synapse.util.clock import Clock

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
        self.state_store = hs.get_datastores().state
        self.state_deletion_store = hs.get_datastores().state_deletion
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

    def test_purge_history_deletes_state_groups(self) -> None:
        """Test that unreferenced state groups get cleaned up after purge"""

        # Send four state changes to the room.
        first = self.helper.send_state(
            self.room_id, event_type="m.foo", body={"test": 1}
        )
        second = self.helper.send_state(
            self.room_id, event_type="m.foo", body={"test": 2}
        )
        third = self.helper.send_state(
            self.room_id, event_type="m.foo", body={"test": 3}
        )
        last = self.helper.send_state(
            self.room_id, event_type="m.foo", body={"test": 4}
        )

        # Get references to the state groups
        event_to_groups = self.get_success(
            self.store._get_state_group_for_events(
                [
                    first["event_id"],
                    second["event_id"],
                    third["event_id"],
                    last["event_id"],
                ]
            )
        )

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

        # Advance so that the background jobs to delete the state groups runs
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # We expect all the state groups associated with events above, except
        # the last one, should return no state.
        state_groups = self.get_success(
            self.state_store._get_state_groups_from_groups(
                list(event_to_groups.values()), StateFilter.all()
            )
        )
        first_state = state_groups[event_to_groups[first["event_id"]]]
        second_state = state_groups[event_to_groups[second["event_id"]]]
        third_state = state_groups[event_to_groups[third["event_id"]]]
        last_state = state_groups[event_to_groups[last["event_id"]]]

        self.assertEqual(first_state, {})
        self.assertEqual(second_state, {})
        self.assertEqual(third_state, {})
        self.assertNotEqual(last_state, {})

    def test_purge_unreferenced_state_group(self) -> None:
        """Test that purging a room also gets rid of unreferenced state groups
        it encounters during the purge.

        This is important, as otherwise these unreferenced state groups get
        "de-deltaed" during the purge process, consuming lots of disk space.
        """

        self.helper.send(self.room_id, body="test1")
        state1 = self.helper.send_state(
            self.room_id, "org.matrix.test", body={"number": 2}
        )
        state2 = self.helper.send_state(
            self.room_id, "org.matrix.test", body={"number": 3}
        )
        self.helper.send(self.room_id, body="test4")
        last = self.helper.send(self.room_id, body="test5")

        # Create an unreferenced state group that has a prev group of one of the
        # to-be-purged events.
        prev_group = self.get_success(
            self.store._get_state_group_for_event(state1["event_id"])
        )
        unreferenced_state_group = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=prev_group,
                delta_ids={("org.matrix.test", ""): state2["event_id"]},
                current_state_ids=None,
            )
        )

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

        # Advance so that the background jobs to delete the state groups runs
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # We expect that the unreferenced state group has been deleted from all tables.
        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_groups",
                keyvalues={"id": unreferenced_state_group},
                retcol="id",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)

        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_groups_state",
                keyvalues={"state_group": unreferenced_state_group},
                retcol="state_group",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)

        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_group_edges",
                keyvalues={"state_group": unreferenced_state_group},
                retcol="state_group",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)

        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_groups_pending_deletion",
                keyvalues={"state_group": unreferenced_state_group},
                retcol="state_group",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)

        # We expect there to now only be one state group for the room, which is
        # the state group of the last event (as the only outlier).
        state_groups = self.get_success(
            self.state_store.db_pool.simple_select_onecol(
                table="state_groups",
                keyvalues={"room_id": self.room_id},
                retcol="id",
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertEqual(len(state_groups), 1)

    def test_clear_unreferenced_state_groups(self) -> None:
        """Test that any unreferenced state groups are automatically cleaned up."""

        self.helper.send(self.room_id, body="test1")
        state1 = self.helper.send_state(
            self.room_id, "org.matrix.test", body={"number": 2}
        )
        # Create enough state events to require multiple batches of
        # mark_unreferenced_state_groups_for_deletion_bg_update to be run.
        for i in range(200):
            self.helper.send_state(self.room_id, "org.matrix.test", body={"number": i})
        self.helper.send(self.room_id, body="test4")
        last = self.helper.send(self.room_id, body="test5")

        # Create an unreferenced state group that has no prev group.
        unreferenced_free_state_group = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=None,
                delta_ids={("org.matrix.test", ""): state1["event_id"]},
                current_state_ids={("org.matrix.test", ""): ""},
            )
        )

        # Create some unreferenced state groups that have a prev group of one of the
        # existing state groups.
        prev_group = self.get_success(
            self.store._get_state_group_for_event(state1["event_id"])
        )
        unreferenced_end_state_group = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=prev_group,
                delta_ids={("org.matrix.test", ""): state1["event_id"]},
                current_state_ids=None,
            )
        )
        another_unreferenced_end_state_group = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=unreferenced_end_state_group,
                delta_ids={("org.matrix.test", ""): state1["event_id"]},
                current_state_ids=None,
            )
        )

        # Add some other unreferenced state groups which lead to a referenced state
        # group.
        # These state groups should not get deleted.
        chain_state_group = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=None,
                delta_ids={("org.matrix.test", ""): ""},
                current_state_ids={("org.matrix.test", ""): ""},
            )
        )
        chain_state_group_2 = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=chain_state_group,
                delta_ids={("org.matrix.test", ""): ""},
                current_state_ids=None,
            )
        )
        referenced_chain_state_group = self.get_success(
            self.state_store.store_state_group(
                event_id=last["event_id"],
                room_id=self.room_id,
                prev_group=chain_state_group_2,
                delta_ids={("org.matrix.test", ""): ""},
                current_state_ids=None,
            )
        )
        self.get_success(
            self.store.db_pool.simple_insert(
                "event_to_state_groups",
                {
                    "event_id": "$new_event",
                    "state_group": referenced_chain_state_group,
                },
            )
        )

        # Insert and run the background update.
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _BackgroundUpdates.MARK_UNREFERENCED_STATE_GROUPS_FOR_DELETION_BG_UPDATE,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False
        self.wait_for_background_updates()

        # Advance so that the background job to delete the state groups runs
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # We expect that the unreferenced free state group has been deleted.
        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_groups",
                keyvalues={"id": unreferenced_free_state_group},
                retcol="id",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)

        # We expect that both unreferenced end state groups have been deleted.
        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_groups",
                keyvalues={"id": unreferenced_end_state_group},
                retcol="id",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)
        row = self.get_success(
            self.state_store.db_pool.simple_select_one_onecol(
                table="state_groups",
                keyvalues={"id": another_unreferenced_end_state_group},
                retcol="id",
                allow_none=True,
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertIsNone(row)

        # We expect there to now only be one state group for the room, which is
        # the state group of the last event (as the only outlier).
        state_groups = self.get_success(
            self.state_store.db_pool.simple_select_onecol(
                table="state_groups",
                keyvalues={"room_id": self.room_id},
                retcol="id",
                desc="test_purge_unreferenced_state_group",
            )
        )
        self.assertEqual(len(state_groups), 210)
