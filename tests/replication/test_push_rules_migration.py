#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from synapse.api.constants import PushRuleIds
from synapse.storage.databases.main.push_rule import (
    _MIGRATE_LEGACY_MENTION_PUSH_RULES_UPDATE_NAME,
)

from tests.replication._base import BaseMultiWorkerStreamTestCase


class LegacyMentionPushRulesMigrationReplicationTestCase(BaseMultiWorkerStreamTestCase):
    """The `migrate_legacy_mention_push_rules` background update runs on a
    worker which is not the push rules writer, and hands each batch to the
    writer over replication."""

    def default_config(self) -> dict:
        conf = super().default_config()
        conf["stream_writers"] = {"push_rules": ["push_writer"]}
        conf["instance_map"] = {
            "main": {"host": "testserv", "port": 8765},
            "push_writer": {"host": "testserv", "port": 1001},
        }
        return conf

    def test_batches_are_written_by_the_push_rules_writer(self) -> None:
        """Running the update on the main process (not a writer) migrates the
        rules and records the change on the push rules stream."""
        writer_hs = self.make_worker_hs(
            "synapse.app.generic_worker", {"worker_name": "push_writer"}
        )
        writer_store = writer_hs.get_datastores().main
        main_store = self.hs.get_datastores().main

        user_id = "@alice:test"
        self.get_success(
            writer_store.set_push_rule_enabled(
                user_id, PushRuleIds.ROOMNOTIF, False, is_default_rule=True
            )
        )
        self.replicate()
        stream_id_before = main_store.get_max_push_rules_stream_id()

        self.get_success(
            main_store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _MIGRATE_LEGACY_MENTION_PUSH_RULES_UPDATE_NAME,
                    "progress_json": "{}",
                },
            )
        )
        updater = main_store.db_pool.updates
        updater._all_done = False
        while not self.get_success(updater.has_completed_background_updates()):
            self.get_success(updater.do_next_background_update(False))
        self.replicate()

        rules = self.get_success(main_store.get_push_rules_for_user(user_id))
        enabled = {rule.rule_id: enabled for rule, enabled in rules.rules()}
        self.assertFalse(enabled[PushRuleIds.IS_ROOM_MENTION])
        self.assertTrue(
            self.get_success(
                main_store.have_push_rules_changed_for_user(user_id, stream_id_before)
            )
        )
