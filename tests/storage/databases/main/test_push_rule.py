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

from collections.abc import Mapping
from typing import Any

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import AccountDataTypes, PushRuleIds
from synapse.rest import admin
from synapse.rest.client import login, sync
from synapse.server import HomeServer
from synapse.storage.databases.main.push_rule import (
    _MIGRATE_LEGACY_MENTION_PUSH_RULES_UPDATE_NAME,
)
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase

NOTIFY_ONLY: list[str | JsonDict] = ["notify"]
NOTIFY_LOUDLY: list[str | JsonDict] = [
    "notify",
    {"set_tweak": "sound", "value": "default"},
]

# The default actions of the mention rules.
USER_MENTION_DEFAULT_ACTIONS: list[str | JsonDict] = [
    "notify",
    {"set_tweak": "highlight"},
    {"set_tweak": "sound", "value": "default"},
]
ROOM_MENTION_DEFAULT_ACTIONS: list[str | JsonDict] = [
    "notify",
    {"set_tweak": "highlight"},
]

# With the fake clock, every batch appears to take no time at all, so after the
# first batch the updater falls back to the minimum batch size: pinning both to
# the same value makes the number of batches deterministic.
BATCH_SIZE = 2


class LegacyMentionPushRulesMigrationTestCase(HomeserverTestCase):
    """Tests the background update which carries customisations of the legacy
    mention rules (removed by MSC4210) over to the intentional mention rules."""

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["background_updates"] = {
            "default_batch_size": BATCH_SIZE,
            "min_batch_size": BATCH_SIZE,
        }
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main

    def _set_enabled(self, user_id: str, rule_id: str, enabled: bool) -> None:
        self.get_success(
            self.store.set_push_rule_enabled(
                user_id, rule_id, enabled, is_default_rule=True
            )
        )

    def _set_actions(
        self, user_id: str, rule_id: str, actions: list[str | JsonDict]
    ) -> None:
        self.get_success(
            self.store.set_push_rule_actions(
                user_id, rule_id, actions, is_default_rule=True
            )
        )

    def _get_rule(
        self, user_id: str, rule_id: str
    ) -> tuple[bool, list[str | Mapping[str, Any]]]:
        """Returns the effective enabled state and actions of a rule, as seen
        by the push rule evaluator."""
        rules = self.get_success(self.store.get_push_rules_for_user(user_id))
        for rule, enabled in rules.rules():
            if rule.rule_id == rule_id:
                return enabled, list(rule.actions)
        raise AssertionError(f"rule {rule_id} not found for {user_id}")

    def _schedule_migration(self) -> None:
        self.get_success(
            self.store.db_pool.simple_insert(
                "background_updates",
                {
                    "update_name": _MIGRATE_LEGACY_MENTION_PUSH_RULES_UPDATE_NAME,
                    "progress_json": "{}",
                },
            )
        )
        self.store.db_pool.updates._all_done = False

    def _run_migration(self) -> int:
        """Runs the background update to completion, returning the number of
        batches it took."""
        self._schedule_migration()
        updater = self.store.db_pool.updates

        batches = 0
        while not self.get_success(updater.has_completed_background_updates()):
            self.get_success(updater.do_next_background_update(False))
            batches += 1

        return batches

    def test_room_mention(self) -> None:
        """Customisations of `.m.rule.roomnotif` are copied onto
        `.m.rule.is_room_mention`."""
        user_id = "@alice:test"
        self._set_enabled(user_id, PushRuleIds.ROOMNOTIF, False)
        self._set_actions(user_id, PushRuleIds.ROOMNOTIF, NOTIFY_ONLY)

        # Warm the cache, so that we also check it gets invalidated.
        self.assertEqual(
            self._get_rule(user_id, PushRuleIds.IS_ROOM_MENTION),
            (True, ROOM_MENTION_DEFAULT_ACTIONS),
        )

        self._run_migration()

        self.assertEqual(
            self._get_rule(user_id, PushRuleIds.IS_ROOM_MENTION), (False, NOTIFY_ONLY)
        )

    def test_user_mention_disabled_only_if_both_legacy_rules_disabled(self) -> None:
        """`.m.rule.is_user_mention` is only disabled for users who had disabled
        both `.m.rule.contains_display_name` and `.m.rule.contains_user_name`."""
        both = "@both:test"
        self._set_enabled(both, PushRuleIds.CONTAINS_DISPLAY_NAME, False)
        self._set_enabled(both, PushRuleIds.CONTAINS_USER_NAME, False)

        only_display_name = "@displayname:test"
        self._set_enabled(only_display_name, PushRuleIds.CONTAINS_DISPLAY_NAME, False)

        only_user_name = "@username:test"
        self._set_enabled(only_user_name, PushRuleIds.CONTAINS_USER_NAME, False)

        # Explicitly re-enabled rules are not "disabled".
        re_enabled = "@reenabled:test"
        self._set_enabled(re_enabled, PushRuleIds.CONTAINS_DISPLAY_NAME, False)
        self._set_enabled(re_enabled, PushRuleIds.CONTAINS_USER_NAME, True)

        self._run_migration()

        self.assertFalse(self._get_rule(both, PushRuleIds.IS_USER_MENTION)[0])
        self.assertTrue(
            self._get_rule(only_display_name, PushRuleIds.IS_USER_MENTION)[0]
        )
        self.assertTrue(self._get_rule(only_user_name, PushRuleIds.IS_USER_MENTION)[0])
        self.assertTrue(self._get_rule(re_enabled, PushRuleIds.IS_USER_MENTION)[0])

    def test_user_mention_actions(self) -> None:
        """Custom actions on either legacy user mention rule are copied onto
        `.m.rule.is_user_mention`, with `.m.rule.contains_display_name` taking
        precedence when both were customised."""
        both = "@both:test"
        self._set_actions(both, PushRuleIds.CONTAINS_DISPLAY_NAME, NOTIFY_ONLY)
        self._set_actions(both, PushRuleIds.CONTAINS_USER_NAME, NOTIFY_LOUDLY)

        only_display_name = "@displayname:test"
        self._set_actions(
            only_display_name, PushRuleIds.CONTAINS_DISPLAY_NAME, NOTIFY_ONLY
        )

        only_user_name = "@username:test"
        self._set_actions(only_user_name, PushRuleIds.CONTAINS_USER_NAME, NOTIFY_LOUDLY)

        self._run_migration()

        self.assertEqual(
            self._get_rule(both, PushRuleIds.IS_USER_MENTION), (True, NOTIFY_ONLY)
        )
        self.assertEqual(
            self._get_rule(only_display_name, PushRuleIds.IS_USER_MENTION),
            (True, NOTIFY_ONLY),
        )
        self.assertEqual(
            self._get_rule(only_user_name, PushRuleIds.IS_USER_MENTION),
            (True, NOTIFY_LOUDLY),
        )

    def test_existing_customisations_of_mention_rules_are_kept(self) -> None:
        """A user's own customisation of a mention rule wins over anything
        derived from the legacy rules."""
        user_id = "@alice:test"
        self._set_enabled(user_id, PushRuleIds.ROOMNOTIF, False)
        self._set_enabled(user_id, PushRuleIds.IS_ROOM_MENTION, True)
        self._set_actions(user_id, PushRuleIds.ROOMNOTIF, NOTIFY_ONLY)
        self._set_actions(user_id, PushRuleIds.IS_ROOM_MENTION, NOTIFY_LOUDLY)

        self._set_enabled(user_id, PushRuleIds.CONTAINS_DISPLAY_NAME, False)
        self._set_enabled(user_id, PushRuleIds.CONTAINS_USER_NAME, False)
        self._set_enabled(user_id, PushRuleIds.IS_USER_MENTION, True)
        self._set_actions(user_id, PushRuleIds.CONTAINS_DISPLAY_NAME, NOTIFY_ONLY)
        self._set_actions(user_id, PushRuleIds.IS_USER_MENTION, NOTIFY_LOUDLY)

        self._run_migration()

        self.assertEqual(
            self._get_rule(user_id, PushRuleIds.IS_ROOM_MENTION), (True, NOTIFY_LOUDLY)
        )
        self.assertEqual(
            self._get_rule(user_id, PushRuleIds.IS_USER_MENTION), (True, NOTIFY_LOUDLY)
        )

    def test_legacy_overrides_are_left_in_place(self) -> None:
        """The legacy overrides are not deleted, so that they still apply
        while the legacy rules are served."""
        user_id = "@alice:test"
        self._set_enabled(user_id, PushRuleIds.ROOMNOTIF, False)
        self._set_actions(user_id, PushRuleIds.CONTAINS_DISPLAY_NAME, NOTIFY_ONLY)

        self._run_migration()

        self.assertEqual(
            self.get_success(self.store.get_push_rules_enabled_for_user(user_id)),
            {
                PushRuleIds.ROOMNOTIF: False,
                PushRuleIds.IS_ROOM_MENTION: False,
                # Setting actions on a rule also records it as enabled.
                PushRuleIds.CONTAINS_DISPLAY_NAME: True,
                PushRuleIds.IS_USER_MENTION: True,
            },
        )
        self.assertIncludes(
            set(
                self.get_success(
                    self.store.db_pool.simple_select_onecol(
                        "push_rules", {"user_name": user_id}, "rule_id"
                    )
                ),
            ),
            {PushRuleIds.CONTAINS_DISPLAY_NAME, PushRuleIds.IS_USER_MENTION},
            exact=True,
        )

    def test_batching(self) -> None:
        """Users are processed in batches, including those whose only
        customisation is an `enabled` override."""
        users = [f"@user{i}:test" for i in range(5)]
        for user_id in users[:3]:
            self._set_actions(user_id, PushRuleIds.ROOMNOTIF, NOTIFY_ONLY)
        for user_id in users[3:]:
            self._set_enabled(user_id, PushRuleIds.ROOMNOTIF, False)
        # A user with no legacy customisation is left alone.
        self._set_enabled("@other:test", PushRuleIds.IS_USER_MENTION, False)

        self.assertEqual(self._run_migration(), 3)

        for user_id in users[:3]:
            self.assertEqual(
                self._get_rule(user_id, PushRuleIds.IS_ROOM_MENTION),
                (True, NOTIFY_ONLY),
            )
        for user_id in users[3:]:
            self.assertEqual(
                self._get_rule(user_id, PushRuleIds.IS_ROOM_MENTION),
                (False, ROOM_MENTION_DEFAULT_ACTIONS),
            )
        self.assertEqual(
            self._get_rule("@other:test", PushRuleIds.IS_ROOM_MENTION),
            (True, ROOM_MENTION_DEFAULT_ACTIONS),
        )

    def test_batching_counts_users_not_rows(self) -> None:
        """A user with several legacy customisations must not shorten a batch,
        as a short batch is what marks the update as complete."""
        users = [f"@user{i}:test" for i in range(BATCH_SIZE + 1)]
        for user_id in users:
            # Two `push_rules` rows (and two `push_rules_enable` rows) per user.
            self._set_actions(user_id, PushRuleIds.CONTAINS_DISPLAY_NAME, NOTIFY_ONLY)
            self._set_actions(user_id, PushRuleIds.CONTAINS_USER_NAME, NOTIFY_LOUDLY)

        self.assertEqual(self._run_migration(), 2)

        for user_id in users:
            self.assertEqual(
                self._get_rule(user_id, PushRuleIds.IS_USER_MENTION),
                (True, NOTIFY_ONLY),
            )

    def test_nothing_to_migrate(self) -> None:
        """The update completes immediately when no user customised the legacy
        rules."""
        self._set_enabled("@alice:test", PushRuleIds.IS_USER_MENTION, False)

        self.assertEqual(self._run_migration(), 1)
        self.assertEqual(
            self._get_rule("@alice:test", PushRuleIds.IS_USER_MENTION),
            (False, USER_MENTION_DEFAULT_ACTIONS),
        )

    def _sync_push_rules(
        self, access_token: str, since: str | None = None
    ) -> tuple[str, list[JsonDict]]:
        """Syncs, returning the next batch token and the `m.push_rules` account
        data events in the response."""
        url = "/sync" if since is None else f"/sync?since={since}"
        channel = self.make_request("GET", url, access_token=access_token)
        self.assertEqual(channel.code, 200, channel.json_body)
        events = channel.json_body.get("account_data", {}).get("events", [])
        return (
            channel.json_body["next_batch"],
            [
                ev["content"]
                for ev in events
                if ev["type"] == AccountDataTypes.PUSH_RULES
            ],
        )

    def test_migrated_rules_reach_clients_through_incremental_sync(self) -> None:
        """The writer records each migrated override on the push rules stream,
        so a client which is already syncing receives the new rules in its next
        incremental sync rather than on its next full fetch."""
        user_id = self.register_user("alice", "pass")
        access_token = self.login("alice", "pass")
        self._set_enabled(user_id, PushRuleIds.ROOMNOTIF, False)
        since, _ = self._sync_push_rules(access_token)

        self._run_migration()

        _, push_rules = self._sync_push_rules(access_token, since)
        self.assertEqual(len(push_rules), 1, push_rules)
        room_mention = next(
            rule
            for rule in push_rules[0]["global"]["override"]
            if rule["rule_id"] == ".m.rule.is_room_mention"
        )
        self.assertFalse(room_mention["enabled"])

    def test_untouched_users_are_not_woken_by_sync(self) -> None:
        """A user the migration visits without changing anything gets no push
        rules stream entry, so their incremental sync stays empty."""
        user_id = self.register_user("carol", "pass")
        access_token = self.login("carol", "pass")
        # Disabling only one of the legacy user mention rules changes nothing.
        self._set_enabled(user_id, PushRuleIds.CONTAINS_DISPLAY_NAME, False)
        since, _ = self._sync_push_rules(access_token)

        self._run_migration()

        _, push_rules = self._sync_push_rules(access_token, since)
        self.assertEqual(push_rules, [])
