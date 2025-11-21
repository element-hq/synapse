#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#


import logging

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests.test_utils.event_injection import create_event
from tests.unittest import HomeserverTestCase

logger = logging.getLogger(__name__)


class StateDeletionStoreTestCase(HomeserverTestCase):
    """Tests for the StateDeletionStore."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.state_store = hs.get_datastores().state
        self.state_deletion_store = hs.get_datastores().state_deletion
        self.purge_events = hs.get_storage_controllers().purge_events

        # We want to disable the automatic deletion of state groups in the
        # background, so we can do controlled tests.
        self.purge_events._delete_state_loop_call.stop()

        self.user_id = self.register_user("test", "password")
        tok = self.login("test", "password")
        self.room_id = self.helper.create_room_as(self.user_id, tok=tok)

    def check_if_can_be_deleted(self, state_group: int) -> bool:
        """Check if the state group is pending deletion."""

        state_group_to_sequence_number = self.get_success(
            self.state_deletion_store.get_pending_deletions([state_group])
        )

        can_be_deleted = self.get_success(
            self.state_deletion_store.db_pool.runInteraction(
                "test_existing_pending_deletion_is_cleared",
                self.state_deletion_store.get_state_groups_ready_for_potential_deletion_txn,
                state_group_to_sequence_number,
            )
        )

        return state_group in can_be_deleted

    def test_no_deletion(self) -> None:
        """Test that calling persisting_state_group_references is fine if
        nothing is pending deletion"""
        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                sender=self.user_id,
            )
        )

        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )

        self.get_success(ctx_mgr.__aenter__())
        self.get_success(ctx_mgr.__aexit__(None, None, None))

    def test_no_deletion_error(self) -> None:
        """Test that calling persisting_state_group_references is fine if
        nothing is pending deletion, but an error occurs."""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                sender=self.user_id,
            )
        )

        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )

        self.get_success(ctx_mgr.__aenter__())
        self.get_success(ctx_mgr.__aexit__(Exception, Exception("test"), None))

    def test_existing_pending_deletion_is_cleared(self) -> None:
        """Test that the pending deletion flag gets cleared when the state group
        gets persisted."""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        # Mark a state group that we're referencing as pending deletion.
        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group]
            )
        )

        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )

        self.get_success(ctx_mgr.__aenter__())
        self.get_success(ctx_mgr.__aexit__(None, None, None))

        # The pending deletion flag should be cleared
        pending_deletion = self.get_success(
            self.state_deletion_store.db_pool.simple_select_one_onecol(
                table="state_groups_pending_deletion",
                keyvalues={"state_group": context.state_group},
                retcol="1",
                allow_none=True,
                desc="test_existing_pending_deletion_is_cleared",
            )
        )
        self.assertIsNone(pending_deletion)

    def test_pending_deletion_is_cleared_during_persist(self) -> None:
        """Test that the pending deletion flag is cleared when a state group
        gets marked for deletion during persistence"""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )
        self.get_success(ctx_mgr.__aenter__())

        # Mark the state group that we're referencing as pending deletion,
        # *after* we have started persisting.
        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group]
            )
        )

        self.get_success(ctx_mgr.__aexit__(None, None, None))

        # The pending deletion flag should be cleared
        pending_deletion = self.get_success(
            self.state_deletion_store.db_pool.simple_select_one_onecol(
                table="state_groups_pending_deletion",
                keyvalues={"state_group": context.state_group},
                retcol="1",
                allow_none=True,
                desc="test_existing_pending_deletion_is_cleared",
            )
        )
        self.assertIsNone(pending_deletion)

    def test_deletion_check(self) -> None:
        """Test that the `get_state_groups_that_can_be_purged_txn` check is
        correct during different points of the lifecycle of persisting an
        event."""
        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group]
            )
        )

        # We shouldn't be able to delete the state group as not enough time as passed
        can_be_deleted = self.check_if_can_be_deleted(context.state_group)
        self.assertFalse(can_be_deleted)

        # After enough time we can delete the state group
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )
        can_be_deleted = self.check_if_can_be_deleted(context.state_group)
        self.assertTrue(can_be_deleted)

        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )
        self.get_success(ctx_mgr.__aenter__())

        # But once we start persisting we can't delete the state group
        can_be_deleted = self.check_if_can_be_deleted(context.state_group)
        self.assertFalse(can_be_deleted)

        self.get_success(ctx_mgr.__aexit__(None, None, None))

        # The pending deletion flag should remain cleared after persistence has
        # finished.
        can_be_deleted = self.check_if_can_be_deleted(context.state_group)
        self.assertFalse(can_be_deleted)

    def test_deletion_error_during_persistence(self) -> None:
        """Test that state groups remain marked as pending deletion if persisting
        the event fails."""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        # Mark a state group that we're referencing as pending deletion.
        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group]
            )
        )

        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )

        self.get_success(ctx_mgr.__aenter__())
        self.get_success(ctx_mgr.__aexit__(Exception, Exception("test"), None))

        # We should be able to delete the state group after a certain amount of
        # time
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )
        can_be_deleted = self.check_if_can_be_deleted(context.state_group)
        self.assertTrue(can_be_deleted)

    def test_race_between_check_and_insert(self) -> None:
        """Check that we correctly handle the race where we go to delete a
        state group, check that it is unreferenced, and then it becomes
        referenced just before we delete it."""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        # Mark a state group that we're referencing as pending deletion.
        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group]
            )
        )

        # Advance time enough so we can delete the state group
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # Check that we'd be able to delete this state group.
        state_group_to_sequence_number = self.get_success(
            self.state_deletion_store.get_pending_deletions([context.state_group])
        )

        can_be_deleted = self.get_success(
            self.state_deletion_store.db_pool.runInteraction(
                "test_existing_pending_deletion_is_cleared",
                self.state_deletion_store.get_state_groups_ready_for_potential_deletion_txn,
                state_group_to_sequence_number,
            )
        )
        self.assertCountEqual(can_be_deleted, [context.state_group])

        # ... in the real world we'd check that the state group isn't referenced here ...

        # Now we persist the event to reference the state group, *after* we
        # check that the state group wasn't referenced
        ctx_mgr = self.state_deletion_store.persisting_state_group_references(
            [(event, context)]
        )

        self.get_success(ctx_mgr.__aenter__())
        self.get_success(ctx_mgr.__aexit__(Exception, Exception("test"), None))

        # We simulate a pause (required to hit the race)
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # We should no longer be able to delete the state group, without having
        # to recheck if its referenced.
        can_be_deleted = self.get_success(
            self.state_deletion_store.db_pool.runInteraction(
                "test_existing_pending_deletion_is_cleared",
                self.state_deletion_store.get_state_groups_ready_for_potential_deletion_txn,
                state_group_to_sequence_number,
            )
        )
        self.assertCountEqual(can_be_deleted, [])

    def test_remove_ancestors_from_can_delete(self) -> None:
        """Test that if a state group is not ready to be deleted, we also don't
        delete anything that is referenced by it"""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        # Create a new state group that references the one from the event
        new_state_group = self.get_success(
            self.state_store.store_state_group(
                event.event_id,
                event.room_id,
                prev_group=context.state_group,
                delta_ids={},
                current_state_ids=None,
            )
        )

        # Mark them both as pending deletion
        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group, new_state_group]
            )
        )

        # Advance time enough so we can delete the state group so they're both
        # ready for deletion.
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # We can now delete both state groups
        self.assertTrue(self.check_if_can_be_deleted(context.state_group))
        self.assertTrue(self.check_if_can_be_deleted(new_state_group))

        # Use the new_state_group to bump its deletion time
        self.get_success(
            self.state_store.store_state_group(
                event.event_id,
                event.room_id,
                prev_group=new_state_group,
                delta_ids={},
                current_state_ids=None,
            )
        )

        # We should now not be able to delete either of the state groups.
        state_group_to_sequence_number = self.get_success(
            self.state_deletion_store.get_pending_deletions(
                [context.state_group, new_state_group]
            )
        )

        # We shouldn't be able to delete the state group as not enough time has passed
        can_be_deleted = self.get_success(
            self.state_deletion_store.db_pool.runInteraction(
                "test_existing_pending_deletion_is_cleared",
                self.state_deletion_store.get_state_groups_ready_for_potential_deletion_txn,
                state_group_to_sequence_number,
            )
        )
        self.assertCountEqual(can_be_deleted, [])

    def test_newly_referenced_state_group_gets_removed_from_pending(self) -> None:
        """Check that if a state group marked for deletion becomes referenced
        (without being removed from pending deletion table), it gets removed
        from pending deletion table."""

        event, context = self.get_success(
            create_event(
                self.hs,
                room_id=self.room_id,
                type="m.test",
                state_key="",
                sender=self.user_id,
            )
        )
        assert context.state_group is not None

        # Mark a state group that we're referencing as pending deletion.
        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion(
                [context.state_group]
            )
        )

        # Advance time enough so we can delete the state group so they're both
        # ready for deletion.
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # Manually insert into the table to mimic the state group getting used.
        self.get_success(
            self.store.db_pool.simple_insert(
                table="event_to_state_groups",
                values={"state_group": context.state_group, "event_id": event.event_id},
                desc="test_newly_referenced_state_group_gets_removed_from_pending",
            )
        )

        # Manually run the background task to delete pending state groups.
        self.get_success(self.purge_events._delete_state_groups_loop())

        # The pending deletion flag should be cleared...
        pending_deletion = self.get_success(
            self.state_deletion_store.db_pool.simple_select_one_onecol(
                table="state_groups_pending_deletion",
                keyvalues={"state_group": context.state_group},
                retcol="1",
                allow_none=True,
                desc="test_newly_referenced_state_group_gets_removed_from_pending",
            )
        )
        self.assertIsNone(pending_deletion)

        # .. but the state should not have been deleted.
        state = self.get_success(
            self.state_store._get_state_for_groups([context.state_group])
        )
        self.assertGreater(len(state[context.state_group]), 0)
