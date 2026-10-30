#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from http import HTTPStatus
from typing import Any
from unittest.mock import AsyncMock, patch

from parameterized import parameterized

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventContentFields, EventTypes, RelationTypes
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase
from synapse.events.snapshot import EventContext
from synapse.push.bulk_push_rule_evaluator import (
    BulkPushRuleEvaluator,
    _coerce_power_level,
)
from synapse.rest import admin
from synapse.rest.client import knock, login, push_rule, register, room
from synapse.server import HomeServer
from synapse.types import JsonDict, create_requester
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, TestCase, override_config


class TestBulkPushRuleEvaluator(HomeserverTestCase):
    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        push_rule.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        # Create a new user and room.
        self.alice = self.register_user("alice", "pass")
        self.token = self.login(self.alice, "pass")
        self.requester = create_requester(self.alice)

        self.room_id = self.helper.create_room_as(
            # This is deliberately set to V9, because we want to test the logic which
            # handles stringy power levels. Stringy power levels were outlawed in V10.
            self.alice,
            room_version=RoomVersions.V9.identifier,
            tok=self.token,
        )

        self.event_creation_handler = self.hs.get_event_creation_handler()

    @parameterized.expand(
        [
            # The historically-permitted bad values. Alice's notification should be
            # allowed if this threshold is at or below her power level (60)
            ("100", False),
            ("0", True),
            (12.34, True),
            (60.0, True),
            (67.89, False),
            # Values that int(...) would not successfully cast should be ignored.
            # The room notification level should then default to 50, per the spec, so
            # Alice's notification is allowed.
            (None, True),
            # We haven't seen `"room": []` or `"room": {}` in the wild (yet), but
            # let's check them for paranoia's sake.
            ([], True),
            ({}, True),
        ]
    )
    def test_action_for_event_by_user_handles_noninteger_room_power_levels(
        self, bad_room_level: object, should_permit: bool
    ) -> None:
        """We should convert strings in `room` to integers before passing to Rust.

        Test this as follows:
        - Create a room as Alice and invite two other users Bob and Charlie.
        - Set PLs so that Alice has PL 60 and `notifications.room` is set to a bad value.
        - Have Alice create a message notifying @room.
        - Evaluate notification actions for that message. This should not raise.
        - Look in the DB to see if that message triggered a highlight for Bob.

        The test is parameterised with two arguments:
        - the bad power level value for "room", before JSON serisalistion
        - whether Bob should expect the message to be highlighted

        Reproduces https://github.com/matrix-org/synapse/issues/14060.

        A lack of validation: the gift that keeps on giving.
        """
        # Join another user to the room, so that there is someone to see Alice's
        # @room notification.
        bob = self.register_user("bob", "pass")
        bob_token = self.login(bob, "pass")
        self.helper.join(self.room_id, bob, tok=bob_token)

        # Alter the power levels in that room to include the bad @room notification
        # level. We need to suppress
        #
        # - canonicaljson validation, because canonicaljson forbids floats;
        # - the event jsonschema validation, because it will forbid bad values; and
        # - the auth rules checks, because they stop us from creating power levels
        #   with `"room": null`. (We want to test this case, because we have seen it
        #   in the wild.)
        #
        # We have seen stringy and null values for "room" in the wild, so presumably
        # some of this validation was missing in the past.
        with (
            patch("synapse.events.validator.validate_canonicaljson"),
            patch("synapse.events.validator.jsonschema.validate"),
            patch("synapse.handlers.event_auth.check_state_dependent_auth_rules"),
        ):
            pl_event_id = self.helper.send_state(
                self.room_id,
                "m.room.power_levels",
                {
                    "users": {self.alice: 60},
                    "notifications": {"room": bad_room_level},
                },
                self.token,
                state_key="",
            )["event_id"]

        # Create a new message event, and try to evaluate it under the dodgy
        # power level event.
        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": "m.room.message",
                    "room_id": self.room_id,
                    "content": {
                        "msgtype": "m.text",
                        "body": "helo @room",
                        EventContentFields.MENTIONS: {"room": True},
                    },
                    "sender": self.alice,
                },
                prev_event_ids=[pl_event_id],
            )
        )
        context = self.get_success(unpersisted_context.persist(event))

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        # should not raise
        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))

        # Did Bob see Alice's @room notification?
        highlighted_actions = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_list(
                table="event_push_actions_staging",
                keyvalues={
                    "event_id": event.event_id,
                    "user_id": bob,
                    "highlight": 1,
                },
                retcols=("*",),
                desc="get_event_push_actions_staging",
            )
        )
        self.assertEqual(len(highlighted_actions), int(should_permit))

    @override_config({"push": {"enabled": False}})
    def test_action_for_event_by_user_disabled_by_config(self) -> None:
        """Ensure that push rules are not calculated when disabled in the config"""

        # Create a new message event which should cause a notification.
        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": "m.room.message",
                    "room_id": self.room_id,
                    "content": {
                        "msgtype": "m.text",
                        "body": "helo",
                    },
                    "sender": self.alice,
                },
            )
        )
        context = self.get_success(unpersisted_context.persist(event))

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        # Mock the method which calculates push rules -- we do this instead of
        # e.g. checking the results in the database because we want to ensure
        # that code isn't even running.
        bulk_evaluator._action_for_event_by_user = AsyncMock()  # type: ignore[method-assign]

        # Ensure no actions are generated!
        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))
        bulk_evaluator._action_for_event_by_user.assert_not_called()

    def _create_message_from_alice(self) -> tuple[EventBase, EventContext]:
        """Create (but do not persist) a message event sent by Alice."""
        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": "m.room.message",
                    "room_id": self.room_id,
                    "content": {"msgtype": "m.text", "body": "helo"},
                    "sender": self.alice,
                },
            )
        )
        context = self.get_success(unpersisted_context.persist(event))
        return event, context

    def _get_users_with_staged_push_actions(self, event_id: str) -> list[str]:
        return self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_onecol(
                table="event_push_actions_staging",
                keyvalues={"event_id": event_id},
                retcol="user_id",
                desc="get_users_with_staged_push_actions",
            )
        )

    def test_action_for_event_by_user_skipped_when_sender_is_only_local_user(
        self,
    ) -> None:
        """Ensure that we don't evaluate push rules when the only local user in the
        room is the sender, as users are never notified about their own events."""
        event, context = self._create_message_from_alice()

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        # The sender should never be considered for notifications.
        rules_by_user = self.get_success(bulk_evaluator._get_rules_for_event(event))
        self.assertEqual(rules_by_user, {})

        # Mock one of the methods called when evaluating push rules -- we do this
        # instead of only checking the results in the database because we want to
        # ensure that code isn't even running.
        bulk_evaluator._get_power_levels_and_sender_level = AsyncMock()  # type: ignore[method-assign]

        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))
        bulk_evaluator._get_power_levels_and_sender_level.assert_not_called()
        self.assertEqual(self._get_users_with_staged_push_actions(event.event_id), [])

    def test_action_for_event_by_user_with_other_local_user(self) -> None:
        """Ensure that push rules are still evaluated for local users other than the
        sender."""
        bob = self.register_user("bob", "pass")
        bob_token = self.login(bob, "pass")
        self.helper.join(self.room_id, bob, tok=bob_token)

        event, context = self._create_message_from_alice()

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        # Only Bob should be considered for notifications.
        rules_by_user = self.get_success(bulk_evaluator._get_rules_for_event(event))
        self.assertEqual(set(rules_by_user), {bob})

        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))
        self.assertEqual(
            self._get_users_with_staged_push_actions(event.event_id), [bob]
        )

    def _create_and_process(
        self,
        bulk_evaluator: BulkPushRuleEvaluator,
        content: JsonDict | None = None,
        type: str = "test",
    ) -> bool:
        """Returns true iff the `mentions` trigger an event push action."""
        # Create a new message event which should cause a notification.
        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": type,
                    "room_id": self.room_id,
                    "content": content or {},
                    "sender": f"@bob:{self.hs.hostname}",
                },
            )
        )
        context = self.get_success(unpersisted_context.persist(event))
        # Execute the push rule machinery.
        self.get_success(bulk_evaluator.action_for_events_by_user([(event, context)]))

        # If any actions are generated for this event, return true.
        result = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_list(
                table="event_push_actions_staging",
                keyvalues={"event_id": event.event_id},
                retcols=("*",),
                desc="get_event_push_actions_staging",
            )
        )
        return len(result) > 0

    def test_legacy_mention_rules_removed(self) -> None:
        """
        Matrix v1.17 (MSC4210) removed the legacy mention rules from the base rule
        set: an event which merely contains the user's name in its body, without
        an `m.mentions` property, must no longer notify.
        """
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        # Note that the events are not `m.room.message`s, so that the generic
        # underride rules do not fire and only the mention rules can notify.
        self.assertFalse(
            self._create_and_process(bulk_evaluator, {"body": "hello alice"}),
            "alice should not be notified (legacy mention rules are removed)",
        )
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "body": "hello alice",
                    EventContentFields.MENTIONS: {"user_ids": [self.alice]},
                },
            ),
            "alice should be notified (intentional mention)",
        )

    @override_config({"experimental_features": {"msc4210_enabled": False}})
    def test_legacy_mention_rules_opt_out(self) -> None:
        """
        The legacy mention rules can temporarily be restored, in which case an
        event which contains the user's name in its body notifies again.
        """
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        self.assertTrue(
            self._create_and_process(bulk_evaluator, {"body": "hello alice"}),
            "alice should be notified (legacy mention)",
        )

    def test_user_mentions(self) -> None:
        """Test the behavior of an event which includes invalid user mentions."""
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        # Not including the mentions field should not notify.
        self.assertFalse(self._create_and_process(bulk_evaluator))
        # An empty mentions field should not notify.
        self.assertFalse(
            self._create_and_process(bulk_evaluator, {EventContentFields.MENTIONS: {}})
        )

        # Non-dict mentions should be ignored.
        #
        # Avoid C-S validation as these aren't expected.
        with patch(
            "synapse.events.validator.EventValidator.validate_new",
            new=lambda s, event, config: True,
        ):
            mentions: Any
            for mentions in (None, True, False, 1, "foo", []):
                self.assertFalse(
                    self._create_and_process(
                        bulk_evaluator, {EventContentFields.MENTIONS: mentions}
                    )
                )

            # A non-list should be ignored.
            for mentions in (None, True, False, 1, "foo", {}):
                self.assertFalse(
                    self._create_and_process(
                        bulk_evaluator,
                        {EventContentFields.MENTIONS: {"user_ids": mentions}},
                    )
                )

        # The Matrix ID appearing anywhere in the list should notify.
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {EventContentFields.MENTIONS: {"user_ids": [self.alice]}},
            )
        )
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    EventContentFields.MENTIONS: {
                        "user_ids": ["@another:test", self.alice]
                    }
                },
            )
        )

        # Duplicate user IDs should notify.
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {EventContentFields.MENTIONS: {"user_ids": [self.alice, self.alice]}},
            )
        )

        # Invalid entries in the list are ignored.
        #
        # Avoid C-S validation as these aren't expected.
        with patch(
            "synapse.events.validator.EventValidator.validate_new",
            new=lambda s, event, config: True,
        ):
            self.assertFalse(
                self._create_and_process(
                    bulk_evaluator,
                    {
                        EventContentFields.MENTIONS: {
                            "user_ids": [None, True, False, {}, []]
                        }
                    },
                )
            )
            self.assertTrue(
                self._create_and_process(
                    bulk_evaluator,
                    {
                        EventContentFields.MENTIONS: {
                            "user_ids": [None, True, False, {}, [], self.alice]
                        }
                    },
                )
            )

        # The legacy push rule should not mention if the mentions field exists.
        self.assertFalse(
            self._create_and_process(
                bulk_evaluator,
                {
                    "body": self.alice,
                    "msgtype": "m.text",
                    EventContentFields.MENTIONS: {},
                },
            )
        )

    def test_room_mentions(self) -> None:
        """Test the behavior of an event which includes invalid room mentions."""
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        # Room mentions from those without power should not notify.
        self.assertFalse(
            self._create_and_process(
                bulk_evaluator, {EventContentFields.MENTIONS: {"room": True}}
            )
        )

        # Room mentions from those with power should notify.
        self.helper.send_state(
            self.room_id,
            "m.room.power_levels",
            {"notifications": {"room": 0}},
            self.token,
            state_key="",
        )
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator, {EventContentFields.MENTIONS: {"room": True}}
            )
        )

        # Invalid data should not notify.
        #
        # Avoid C-S validation as these aren't expected.
        with patch(
            "synapse.events.validator.EventValidator.validate_new",
            new=lambda s, event, config: True,
        ):
            mentions: Any
            for mentions in (None, False, 1, "foo", [], {}):
                self.assertFalse(
                    self._create_and_process(
                        bulk_evaluator,
                        {EventContentFields.MENTIONS: {"room": mentions}},
                    )
                )

        # The legacy push rule should not mention if the mentions field exists.
        self.assertFalse(
            self._create_and_process(
                bulk_evaluator,
                {
                    "body": "@room",
                    "msgtype": "m.text",
                    EventContentFields.MENTIONS: {},
                },
            )
        )

    def test_suppress_edits(self) -> None:
        """Under the default push rules, event edits should not generate notifications."""
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)

        # Create & persist an event to use as the parent of the relation.
        event, unpersisted_context = self.get_success(
            self.event_creation_handler.create_event(
                self.requester,
                {
                    "type": "m.room.message",
                    "room_id": self.room_id,
                    "content": {
                        "msgtype": "m.text",
                        "body": "helo",
                    },
                    "sender": self.alice,
                },
            )
        )
        context = self.get_success(unpersisted_context.persist(event))
        self.get_success(
            self.event_creation_handler.handle_new_client_event(
                self.requester, events_and_context=[(event, context)]
            )
        )

        # The edit should not cause a notification.
        self.assertFalse(
            self._create_and_process(
                bulk_evaluator,
                {
                    "body": "Test message",
                    "m.relates_to": {
                        "rel_type": RelationTypes.REPLACE,
                        "event_id": event.event_id,
                    },
                },
            )
        )

        # An edit which is a mention will cause a notification.
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "body": "Test message",
                    "m.relates_to": {
                        "rel_type": RelationTypes.REPLACE,
                        "event_id": event.event_id,
                    },
                    "m.mentions": {
                        "user_ids": [self.alice],
                    },
                },
            )
        )

    @override_config({"experimental_features": {"msc4306_enabled": True}})
    def test_thread_subscriptions(self) -> None:
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        (thread_root_id,) = self.helper.send_messages(self.room_id, 1, tok=self.token)

        self.assertFalse(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "test message before subscription",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                type=EventTypes.Message,
            )
        )

        self.get_success(
            self.hs.get_datastores().main.subscribe_user_to_thread(
                self.alice,
                self.room_id,
                thread_root_id,
                automatic_event_orderings=None,
            )
        )

        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "test message after subscription",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                type="m.room.message",
            )
        )

    @override_config({"experimental_features": {"msc4306_enabled": True}})
    def test_thread_subscriptions_suppression_after_keyword_mention_overrides(
        self,
    ) -> None:
        """
        Tests one of the purposes of the `postcontent` push rule section:
        When a keyword mention is configured (in the `content` section),
        it does not get suppressed by the thread being unsubscribed.
        """
        # add a keyword mention to alice's push rules
        channel = self.make_request(
            "PUT",
            "/_matrix/client/v3/pushrules/global/content/biscuits",
            {"pattern": "biscuits", "actions": ["notify"]},
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        (thread_root_id,) = self.helper.send_messages(self.room_id, 1, tok=self.token)

        self.assertFalse(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "do you want some cookies?",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                type="m.room.message",
            ),
            "alice is not subscribed to thread and does not have a mention on 'cookies' so should not be notified",
        )

        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "biscuits are available in the kitchen",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                type="m.room.message",
            ),
            "alice is not subscribed to thread but DOES have a mention on 'biscuits' so should be notified",
        )

    @override_config({"experimental_features": {"msc4306_enabled": True}})
    def test_thread_subscriptions_notification_before_keywords_and_mentions(
        self,
    ) -> None:
        """
        Tests one of the purposes of the `postcontent` push rule section:
        When a room is set to (what is commonly known as) 'keywords & mentions', we still receive notifications
        for messages in threads that we are subscribed to.
        Effectively making this 'keywords, mentions & subscriptions'
        """
        # add a 'keywords & mentions' setting to the room alice's push rules
        # In case this rule isn't clear: by adding a rule in the `room` section that does nothing,
        # it stops execution of the push rules before we fall through to the `underride` section,
        # where intuitively many kinds of messages will ambiently generate notifications.
        # Mentions and keywords are triggered before the `room` block, so this doesn't suppress those.
        channel = self.make_request(
            "PUT",
            f"/_matrix/client/v3/pushrules/global/room/{self.room_id}",
            {"actions": []},
            access_token=self.token,
        )
        self.assertEqual(channel.code, HTTPStatus.OK)

        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        (thread_root_id,) = self.helper.send_messages(self.room_id, 1, tok=self.token)

        # sanity check that our mentions still work
        self.assertFalse(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "this is a plain message with no mention",
                },
                type="m.room.message",
            ),
            "alice should not be notified (mentions & keywords room setting)",
        )
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "this is a message that mentions alice",
                    EventContentFields.MENTIONS: {"user_ids": [self.alice]},
                },
                type="m.room.message",
            ),
            "alice should be notified (mentioned)",
        )

        # let's have alice subscribe to the thread
        self.get_success(
            self.hs.get_datastores().main.subscribe_user_to_thread(
                self.alice,
                self.room_id,
                thread_root_id,
                automatic_event_orderings=None,
            )
        )

        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "some message in the thread",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                type="m.room.message",
            ),
            "alice is subscribed to thread so should be notified",
        )

    def test_with_disabled_thread_subscriptions(self) -> None:
        """
        Test what happens with threaded events when MSC4306 is disabled.

        FUTURE: If MSC4306 becomes enabled-by-default/accepted, this test is to be removed.
        """
        bulk_evaluator = BulkPushRuleEvaluator(self.hs)
        (thread_root_id,) = self.helper.send_messages(self.room_id, 1, tok=self.token)

        # When MSC4306 is not enabled, a threaded message generates a notification
        # by default.
        self.assertTrue(
            self._create_and_process(
                bulk_evaluator,
                {
                    "msgtype": "m.text",
                    "body": "test message before subscription",
                    "m.relates_to": {
                        "rel_type": RelationTypes.THREAD,
                        "event_id": thread_root_id,
                    },
                },
                type="m.room.message",
            )
        )

    def _get_rule_ids(self) -> set[str]:
        """Returns the IDs of all push rules that apply to Alice."""
        filtered_push_rules = self.get_success(
            self.hs.get_datastores().main.get_push_rules_for_user(self.alice)
        )
        return {rule.rule_id for rule, _ in filtered_push_rules.rules()}

    def test_msc4075_rules_omitted_when_disabled(self) -> None:
        """The MSC4075 base rules should be filtered out unless the feature is enabled."""
        rule_ids = self._get_rule_ids()
        self.assertNotIn(
            "global/override/.org.matrix.msc4075.rule.rtc.invite_for_me", rule_ids
        )
        self.assertNotIn(
            "global/override/.org.matrix.msc4075.rule.rtc.invite_for_room", rule_ids
        )

    @override_config({"experimental_features": {"msc4075_enabled": True}})
    def test_msc4075_rules_present_when_enabled(self) -> None:
        """The MSC4075 base rules should be included when the feature is enabled."""
        rule_ids = self._get_rule_ids()
        self.assertIn(
            "global/override/.org.matrix.msc4075.rule.rtc.invite_for_me", rule_ids
        )
        self.assertIn(
            "global/override/.org.matrix.msc4075.rule.rtc.invite_for_room", rule_ids
        )


class TestKnockPushRules(HomeserverTestCase):
    """Tests for the MSC4506 knock push rule (`.org.matrix.msc4506.rule.knock`
    with the `recipient_permission` condition): a knock should notify exactly
    the members of the room whose power level lets them act on it.
    """

    servlets = [
        admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        knock.register_servlets,
        login.register_servlets,
        register.register_servlets,
        push_rule.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self.alice = self.register_user("alice", "pass")
        self.alice_token = self.login(self.alice, "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_token = self.login(self.bob, "pass")
        self.charlie = self.register_user("charlie", "pass")
        self.charlie_token = self.login(self.charlie, "pass")

    def _knock_and_get_event_id(
        self,
        power_levels: JsonDict | None = None,
        room_version: str = "11",
    ) -> str:
        """Set up a knockable room and knock on it.

        Alice creates the room and Bob joins it, then Charlie knocks on it.
        `power_levels` becomes the room's `m.room.power_levels` content; by
        default it puts Alice at PL 100 and requires PL 50 to invite, so Alice
        can act on the knock and Bob cannot. Room version 12 and later reject
        a creator listed in `users`, hence the older default `room_version`.
        Returns the knock event id.
        """
        if power_levels is None:
            power_levels = {"users": {self.alice: 100}, "invite": 50}

        room_id = self.helper.create_room_as(
            self.alice,
            is_public=True,
            room_version=room_version,
            tok=self.alice_token,
        )
        self.helper.join(room_id, self.bob, tok=self.bob_token)
        self.helper.send_state(
            room_id,
            "m.room.power_levels",
            power_levels,
            self.alice_token,
        )
        self.helper.send_state(
            room_id,
            "m.room.join_rules",
            {"join_rule": "knock"},
            self.alice_token,
        )
        self.helper.knock(room=room_id, user=self.charlie, tok=self.charlie_token)

        state = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(room_id)
        )
        return state[("m.room.member", self.charlie)].event_id

    def _notified_users(self, event_id: str) -> set[str]:
        """The users with a `notify` push action recorded for the given event."""
        return set(
            self.get_success(
                self.hs.get_datastores().main.db_pool.simple_select_onecol(
                    table="event_push_actions",
                    keyvalues={"event_id": event_id, "notif": 1},
                    retcol="user_id",
                    desc="get_event_push_actions",
                )
            )
        )

    @override_config({"experimental_features": {"msc4506_enabled": True}})
    def test_knock_notifies_only_users_who_can_act(self) -> None:
        """Alice can invite the knocker, so she is notified; Bob cannot, and
        Charlie is never notified of his own knock.
        """
        knock_event_id = self._knock_and_get_event_id()

        self.assertIncludes(
            self._notified_users(knock_event_id), {self.alice}, exact=True
        )

    def test_knock_notifies_nobody_when_disabled(self) -> None:
        """The same knock notifies nobody while `msc4506_enabled` is off."""
        knock_event_id = self._knock_and_get_event_id()

        self.assertIncludes(self._notified_users(knock_event_id), set(), exact=True)

    @override_config({"experimental_features": {"msc4506_enabled": True}})
    def test_knock_notifies_everyone_when_invite_level_absent(self) -> None:
        """An `invite` level absent from `m.room.power_levels` defaults to 0,
        so every member of the room can act on the knock.
        """
        knock_event_id = self._knock_and_get_event_id(
            power_levels={"users": {self.alice: 100}}
        )

        self.assertIncludes(
            self._notified_users(knock_event_id), {self.alice, self.bob}, exact=True
        )

    @override_config({"experimental_features": {"msc4506_enabled": True}})
    def test_knock_notifies_room_creator(self) -> None:
        """A room creator can act on the knock even though room versions with
        MSC4289 leave them out of `m.room.power_levels->users`.
        """
        knock_event_id = self._knock_and_get_event_id(
            power_levels={"invite": 50}, room_version="12"
        )

        self.assertIncludes(
            self._notified_users(knock_event_id), {self.alice}, exact=True
        )

    @override_config({"experimental_features": {"msc4506_enabled": True}})
    def test_knock_notifies_creator_who_is_only_member(self) -> None:
        """A knock on a room whose only joined member is its creator notifies
        the creator. The knocker is not joined, so the evaluator still has a
        local user other than the sender to evaluate rules for.
        """
        room_id = self.helper.create_room_as(
            self.alice, is_public=True, room_version="12", tok=self.alice_token
        )
        self.helper.send_state(
            room_id,
            "m.room.join_rules",
            {"join_rule": "knock"},
            self.alice_token,
        )
        self.helper.knock(room=room_id, user=self.charlie, tok=self.charlie_token)

        state = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state(room_id)
        )
        knock_event_id = state[("m.room.member", self.charlie)].event_id

        self.assertIncludes(
            self._notified_users(knock_event_id), {self.alice}, exact=True
        )


class TestCoercePowerLevel(TestCase):
    """Tests for `_coerce_power_level`, which reads the levels the MSC4506
    `recipient_permission` condition compares against.

    A room whose `m.room.power_levels` holds a null or otherwise non-integer
    level cannot be built locally (`_check_power_levels` rejects one), so this
    covers that federated state directly.
    """

    def test_levels_coerce_to_int_or_to_the_default(self) -> None:
        self.assertEqual(_coerce_power_level(50, 0), 50)
        self.assertEqual(_coerce_power_level("50", 0), 50)
        self.assertEqual(_coerce_power_level(50.5, 0), 50)
        # An explicit null reads as the default, as it does in `get_named_level`.
        self.assertEqual(_coerce_power_level(None, 50), 50)
        self.assertEqual(_coerce_power_level("fifty", 50), 50)
