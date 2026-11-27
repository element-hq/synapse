#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Reading state groups whose deltas hold tombstone rows.

A tombstone is a `state_groups_state` row with a NULL `event_id`. No code
writes them yet and the column is still `NOT NULL`, so these tests relax the
column and insert the rows directly.
"""

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.server import HomeServer
from synapse.storage.database import LoggingTransaction
from synapse.storage.databases.state.store import _GetStateGroupDelta
from synapse.storage.engines import PostgresEngine
from synapse.types import MutableStateMap, StateKey, StateMap
from synapse.types.state import StateFilter
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase

ROOM_ID = "!room:test"

CREATE = (EventTypes.Create, "")
NAME = (EventTypes.Name, "")
TOPIC = (EventTypes.Topic, "")
ALICE = (EventTypes.Member, "@alice:test")
BOB = (EventTypes.Member, "@bob:test")
CAROL = (EventTypes.Member, "@carol:test")

# A delta maps a key to an event ID, or to `None` for a tombstone.
Delta = dict[StateKey, str | None]

# A chain of four groups, each on top of the previous one. Every group except
# the root removes something.
CHAIN: list[Delta] = [
    {CREATE: "$create", NAME: "$name1", ALICE: "$alice", BOB: "$bob"},
    {NAME: None, BOB: None, TOPIC: "$topic"},
    {NAME: "$name2", CAROL: "$carol"},
    {ALICE: None, CAROL: None},
]

# One filter per query shape.
FILTERS = {
    # Postgres reads the whole chain in one query. SQLite walks every group.
    "all": StateFilter.all(),
    # Postgres runs one query per condition. SQLite stops walking once both
    # keys have a row, and a tombstone counts as a row.
    "name+topic": StateFilter.from_types([NAME, TOPIC]),
    # One query per condition, matching on the type alone.
    "members": StateFilter.from_types([(EventTypes.Member, None)]),
    # The whole chain in one query, with a filter clause.
    "alice+others": StateFilter.freeze(
        {EventTypes.Member: {ALICE[1]}}, include_others=True
    ),
}


def fold(deltas: list[Delta]) -> StateMap[str]:
    """The state at the end of a chain of deltas, oldest first."""
    state: MutableStateMap[str] = {}
    for delta in deltas:
        for key, event_id in delta.items():
            if event_id is None:
                state.pop(key, None)
            else:
                state[key] = event_id
    return state


class StateTombstoneReadTestCase(HomeserverTestCase):
    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.state_store = hs.get_datastores().state
        self.state_deletion_store = hs.get_datastores().state_deletion
        self.get_success(
            self.state_store.db_pool.runInteraction(
                "make_event_id_nullable", self._make_event_id_nullable_txn
            )
        )
        self.groups = self._write_chain()

    def _make_event_id_nullable_txn(self, txn: LoggingTransaction) -> None:
        """Drop `NOT NULL` from `state_groups_state.event_id`, as a later schema
        change will."""
        # TODO: Remove this helper once a schema delta makes the column
        # nullable. The asserts below fail then, as a reminder.
        if isinstance(txn.database_engine, PostgresEngine):
            txn.execute(
                "SELECT is_nullable FROM information_schema.columns"
                " WHERE table_name = 'state_groups_state'"
                " AND column_name = 'event_id'"
            )
            assert txn.fetchall() == [("NO",)], "event_id is already nullable"

            txn.execute(
                "ALTER TABLE state_groups_state ALTER COLUMN event_id DROP NOT NULL"
            )
            return

        txn.execute(
            "SELECT \"notnull\" FROM pragma_table_info('state_groups_state')"
            " WHERE name = 'event_id'"
        )
        assert txn.fetchall() == [(1,)], "event_id is already nullable"

        # SQLite cannot drop a constraint, so rebuild the table.
        txn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type = 'index' AND tbl_name = 'state_groups_state'"
            " AND sql IS NOT NULL"
        )
        index_sqls = [sql for (sql,) in txn.fetchall()]
        txn.execute(
            """
            CREATE TABLE state_groups_state_nullable (
                state_group BIGINT NOT NULL,
                room_id TEXT NOT NULL,
                type TEXT NOT NULL,
                state_key TEXT NOT NULL,
                event_id TEXT
            )
            """
        )
        txn.execute(
            "INSERT INTO state_groups_state_nullable"
            " SELECT state_group, room_id, type, state_key, event_id"
            " FROM state_groups_state"
        )
        txn.execute("DROP TABLE state_groups_state")
        txn.execute(
            "ALTER TABLE state_groups_state_nullable RENAME TO state_groups_state"
        )
        for sql in index_sqls:
            txn.execute(sql)

    def _write_chain(self) -> list[int]:
        """Write `CHAIN` as a chain of groups, and return their IDs, oldest
        first."""
        # Each test gets a fresh database with no rooms in it, so no state
        # groups exist yet and any IDs are free. `state_groups.id` is the
        # primary key, so a clash would fail the insert.
        groups = list(range(1000, 1000 + len(CHAIN)))
        db_pool = self.state_store.db_pool

        self.get_success(
            db_pool.simple_insert_many(
                table="state_groups",
                keys=("id", "room_id", "event_id"),
                values=[(group, ROOM_ID, f"$event{group}") for group in groups],
                desc="test_write_chain",
            )
        )
        self.get_success(
            db_pool.simple_insert_many(
                table="state_group_edges",
                keys=("state_group", "prev_state_group"),
                values=list(zip(groups[1:], groups[:-1])),
                desc="test_write_chain",
            )
        )
        self.get_success(
            db_pool.simple_insert_many(
                table="state_groups_state",
                keys=("state_group", "room_id", "type", "state_key", "event_id"),
                values=[
                    (group, ROOM_ID, key[0], key[1], event_id)
                    for group, delta in zip(groups, CHAIN)
                    for key, event_id in delta.items()
                ],
                desc="test_write_chain",
            )
        )
        return groups

    def test_database_reads(self) -> None:
        """Reads leave out tombstoned keys, and do not pick up rows for them
        from earlier in the chain."""
        for name, state_filter in FILTERS.items():
            with self.subTest(filter=name):
                result = self.get_success(
                    self.state_store._get_state_groups_from_groups(
                        self.groups, state_filter
                    )
                )
                for i, group in enumerate(self.groups):
                    self.assertEqual(
                        dict(result[group]),
                        state_filter.filter_state(fold(CHAIN[: i + 1])),
                        f"group {i}",
                    )

    def test_get_state_group_delta(self) -> None:
        """A group whose delta holds a tombstone reports no delta. A group with
        a plain delta reports it as before."""
        self.assertEqual(
            self.get_success(self.state_store.get_state_group_delta(self.groups[1])),
            _GetStateGroupDelta(None, None),
        )
        self.assertEqual(
            self.get_success(self.state_store.get_state_group_delta(self.groups[2])),
            _GetStateGroupDelta(self.groups[1], {NAME: "$name2", CAROL: "$carol"}),
        )

    def test_purge_rewrites_child_without_tombstoned_keys(self) -> None:
        """Purging a group whose delta holds tombstones rewrites its child as a
        full copy. The keys the tombstones removed stay removed."""
        deleted = self.groups[1]

        self.get_success(
            self.state_deletion_store.mark_state_groups_as_pending_deletion([deleted])
        )
        # Advance so that the background job to delete the state group runs.
        self.reactor.advance(
            1 + self.state_deletion_store.DELAY_BEFORE_DELETION_MS / 1000
        )

        # The child is now a full copy: no edge, and no tombstone rows. `BOB`
        # stays absent.
        db_pool = self.state_store.db_pool
        child = self.groups[2]
        edge = self.get_success(
            db_pool.simple_select_one_onecol(
                table="state_group_edges",
                keyvalues={"state_group": child},
                retcol="prev_state_group",
                allow_none=True,
            )
        )
        self.assertIsNone(edge)
        rows = self.get_success(
            db_pool.simple_select_list(
                table="state_groups_state",
                keyvalues={"state_group": child},
                retcols=("type", "state_key", "event_id"),
            )
        )
        self.assertEqual(
            {(typ, state_key): event_id for typ, state_key, event_id in rows},
            fold(CHAIN[:3]),
        )
