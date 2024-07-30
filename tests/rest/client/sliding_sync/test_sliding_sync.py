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
import logging
from typing import Any, Iterable, Literal, Optional, Tuple

from typing_extensions import assert_never

from synapse.events import EventBase
from synapse.types import JsonDict, StreamKeyType, StreamToken
from synapse.util.stringutils import random_string

from tests import unittest

logger = logging.getLogger(__name__)


class SlidingSyncBase(unittest.HomeserverTestCase):
    """Base class for sliding sync test cases"""

    sync_endpoint = "/_matrix/client/unstable/org.matrix.simplified_msc3575/sync"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        # Enable sliding sync
        config["experimental_features"] = {"msc3575_enabled": True}
        return config

    def do_sync(
        self, sync_body: JsonDict, *, since: Optional[str] = None, tok: str
    ) -> Tuple[JsonDict, str]:
        """Do a sliding sync request with given body.

        Asserts the request was successful.

        Attributes:
            sync_body: The full request body to use
            since: Optional since token
            tok: Access token to use

        Returns:
            A tuple of the response body and the `pos` field.
        """

        sync_path = self.sync_endpoint
        if since:
            sync_path += f"?pos={since}"

        channel = self.make_request(
            method="POST",
            path=sync_path,
            content=sync_body,
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        return channel.json_body, channel.json_body["pos"]

    def _assertRequiredStateIncludes(
        self,
        actual_required_state: Any,
        expected_state_events: Iterable[EventBase],
        exact: bool = False,
    ) -> None:
        """
        Wrapper around `assertIncludes` to give slightly better looking diff error
        messages that include some context "$event_id (type, state_key)".

        Args:
            actual_required_state: The "required_state" of a room from a Sliding Sync
                request response.
            expected_state_events: The expected state events to be included in the
                `actual_required_state`.
            exact: Whether the actual state should be exactly equal to the expected
                state (no extras).
        """

        assert isinstance(actual_required_state, list)
        for event in actual_required_state:
            assert isinstance(event, dict)

        self.assertIncludes(
            {
                f'{event["event_id"]} ("{event["type"]}", "{event["state_key"]}")'
                for event in actual_required_state
            },
            {
                f'{event.event_id} ("{event.type}", "{event.state_key}")'
                for event in expected_state_events
            },
            exact=exact,
            # Message to help understand the diff in context
            message=str(actual_required_state),
        )

    def _bump_notifier_wait_for_events(
        self,
        user_id: str,
        wake_stream_key: Literal[
            StreamKeyType.ACCOUNT_DATA,
            StreamKeyType.PRESENCE,
        ],
    ) -> None:
        """
        Wake-up a `notifier.wait_for_events(user_id)` call without affecting the Sliding
        Sync results.

        Args:
            user_id: The user ID to wake up the notifier for
            wake_stream_key: The stream key to wake up. This will create an actual new
                entity in that stream so it's best to choose one that won't affect the
                Sliding Sync results you're testing for. In other words, if your testing
                account data, choose `StreamKeyType.PRESENCE` instead. We support two
                possible stream keys because you're probably testing one or the other so
                one is always a "safe" option.
        """
        # We're expecting some new activity from this point onwards
        from_token = self.hs.get_event_sources().get_current_token()

        triggered_notifier_wait_for_events = False

        async def _on_new_acivity(
            before_token: StreamToken, after_token: StreamToken
        ) -> bool:
            nonlocal triggered_notifier_wait_for_events
            triggered_notifier_wait_for_events = True
            return True

        notifier = self.hs.get_notifier()

        # Listen for some new activity for the user. We're just trying to confirm that
        # our bump below actually does what we think it does (triggers new activity for
        # the user).
        result_awaitable = notifier.wait_for_events(
            user_id,
            1000,
            _on_new_acivity,
            from_token=from_token,
        )

        # Update the account data or presence so that `notifier.wait_for_events(...)`
        # wakes up. We chose these two options because they're least likely to show up
        # in the Sliding Sync response so it won't affect whether we have results.
        if wake_stream_key == StreamKeyType.ACCOUNT_DATA:
            self.get_success(
                self.hs.get_account_data_handler().add_account_data_for_user(
                    user_id,
                    "org.matrix.foobarbaz",
                    {"foo": "bar"},
                )
            )
        elif wake_stream_key == StreamKeyType.PRESENCE:
            sending_user_id = self.register_user(
                "user_bump_notifier_wait_for_events_" + random_string(10), "pass"
            )
            sending_user_tok = self.login(sending_user_id, "pass")
            test_msg = {"foo": "bar"}
            chan = self.make_request(
                "PUT",
                "/_matrix/client/r0/sendToDevice/m.test/1234",
                content={"messages": {user_id: {"d1": test_msg}}},
                access_token=sending_user_tok,
            )
            self.assertEqual(chan.code, 200, chan.result)
        else:
            assert_never(wake_stream_key)

        # Wait for our notifier result
        self.get_success(result_awaitable)

        if not triggered_notifier_wait_for_events:
            raise AssertionError(
                "Expected `notifier.wait_for_events(...)` to be triggered"
            )


# FIXME: Do not merge like this. We need to resolve everything after
# https://github.com/element-hq/synapse/pull/17504 merges.
