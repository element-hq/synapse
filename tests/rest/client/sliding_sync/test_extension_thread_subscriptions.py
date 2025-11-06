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
from http import HTTPStatus
from typing import cast

from twisted.test.proto_helpers import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, room, sync, thread_subscriptions
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase

logger = logging.getLogger(__name__)


# The name of the extension. Currently unstable-prefixed.
EXT_NAME = "io.element.msc4308.thread_subscriptions"


class SlidingSyncThreadSubscriptionsExtensionTestCase(SlidingSyncBase):
    """
    Test the thread subscriptions extension in the Sliding Sync API.
    """

    maxDiff = None

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        thread_subscriptions.register_servlets,
    ]

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4306_enabled": True}
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        super().prepare(reactor, clock, hs)

    def test_no_data_initial_sync(self) -> None:
        """
        Test enabling thread subscriptions extension during initial sync with no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertNotIn(EXT_NAME, response_body["extensions"])

    def test_no_data_incremental_sync(self) -> None:
        """
        Test enabling thread subscriptions extension during incremental sync with no data.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        initial_sync_body: JsonDict = {
            "lists": {},
        }

        # Initial sync
        response_body, sync_pos = self.do_sync(initial_sync_body, tok=user1_tok)

        # Incremental sync with extension enabled
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        response_body, _ = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert
        self.assertNotIn(
            EXT_NAME,
            response_body["extensions"],
            response_body,
        )

    def test_thread_subscription_initial_sync(self) -> None:
        """
        Test thread subscriptions appear in initial sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # get the baseline stream_id of the thread_subscriptions stream
        # before we write any data.
        # Required because the initial value differs between SQLite and Postgres.
        base = self.store.get_max_thread_subscriptions_stream_id()

        self._subscribe_to_thread(user1_id, room_id, thread_root_id)
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {
                "subscribed": {
                    room_id: {
                        thread_root_id: {
                            "automatic": False,
                            "bump_stamp": base + 1,
                        }
                    }
                }
            },
        )

    def test_thread_subscription_incremental_sync(self) -> None:
        """
        Test new thread subscriptions appear in incremental sync response.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # get the baseline stream_id of the thread_subscriptions stream
        # before we write any data.
        # Required because the initial value differs between SQLite and Postgres.
        base = self.store.get_max_thread_subscriptions_stream_id()

        # Initial sync
        _, sync_pos = self.do_sync(sync_body, tok=user1_tok)
        logger.info("Synced to: %r, now subscribing to thread", sync_pos)

        # Subscribe
        self._subscribe_to_thread(user1_id, room_id, thread_root_id)

        # Incremental sync
        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)
        logger.info("Synced to: %r", sync_pos)

        # Assert
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {
                "subscribed": {
                    room_id: {
                        thread_root_id: {
                            "automatic": False,
                            "bump_stamp": base + 1,
                        }
                    }
                }
            },
        )

    def test_unsubscribe_from_thread(self) -> None:
        """
        Test unsubscribing from a thread.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)
        thread_root_resp = self.helper.send(room_id, body="Thread root", tok=user1_tok)
        thread_root_id = thread_root_resp["event_id"]

        # get the baseline stream_id of the thread_subscriptions stream
        # before we write any data.
        # Required because the initial value differs between SQLite and Postgres.
        base = self.store.get_max_thread_subscriptions_stream_id()

        self._subscribe_to_thread(user1_id, room_id, thread_root_id)
        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok)

        # Assert: Subscription present
        self.assertIn(EXT_NAME, response_body["extensions"])
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {
                "subscribed": {
                    room_id: {
                        thread_root_id: {"automatic": False, "bump_stamp": base + 1}
                    }
                }
            },
        )

        # Unsubscribe
        self._unsubscribe_from_thread(user1_id, room_id, thread_root_id)

        # Incremental sync
        response_body, sync_pos = self.do_sync(sync_body, tok=user1_tok, since=sync_pos)

        # Assert: Unsubscription present
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {"unsubscribed": {room_id: {thread_root_id: {"bump_stamp": base + 2}}}},
        )

    def test_multiple_thread_subscriptions(self) -> None:
        """
        Test handling of multiple thread subscriptions.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create thread roots
        thread_root_resp1 = self.helper.send(
            room_id, body="Thread root 1", tok=user1_tok
        )
        thread_root_id1 = thread_root_resp1["event_id"]
        thread_root_resp2 = self.helper.send(
            room_id, body="Thread root 2", tok=user1_tok
        )
        thread_root_id2 = thread_root_resp2["event_id"]
        thread_root_resp3 = self.helper.send(
            room_id, body="Thread root 3", tok=user1_tok
        )
        thread_root_id3 = thread_root_resp3["event_id"]

        # get the baseline stream_id of the thread_subscriptions stream
        # before we write any data.
        # Required because the initial value differs between SQLite and Postgres.
        base = self.store.get_max_thread_subscriptions_stream_id()

        # Subscribe to threads
        self._subscribe_to_thread(user1_id, room_id, thread_root_id1)
        self._subscribe_to_thread(user1_id, room_id, thread_root_id2)
        self._subscribe_to_thread(user1_id, room_id, thread_root_id3)

        sync_body = {
            "lists": {},
            "extensions": {
                EXT_NAME: {
                    "enabled": True,
                }
            },
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        self.assertEqual(
            response_body["extensions"][EXT_NAME],
            {
                "subscribed": {
                    room_id: {
                        thread_root_id1: {
                            "automatic": False,
                            "bump_stamp": base + 1,
                        },
                        thread_root_id2: {
                            "automatic": False,
                            "bump_stamp": base + 2,
                        },
                        thread_root_id3: {
                            "automatic": False,
                            "bump_stamp": base + 3,
                        },
                    }
                }
            },
        )

    def test_limit_parameter(self) -> None:
        """
        Test limit parameter in thread subscriptions extension.
        """
        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # Create 5 thread roots and subscribe to each
        thread_root_ids = []
        for i in range(5):
            thread_root_resp = self.helper.send(
                room_id, body=f"Thread root {i}", tok=user1_tok
            )
            thread_root_ids.append(thread_root_resp["event_id"])
            self._subscribe_to_thread(user1_id, room_id, thread_root_ids[-1])

        sync_body = {
            "lists": {},
            "extensions": {EXT_NAME: {"enabled": True, "limit": 3}},
        }

        # Sync
        response_body, _ = self.do_sync(sync_body, tok=user1_tok)

        # Assert
        thread_subscriptions = response_body["extensions"][EXT_NAME]
        self.assertEqual(
            len(thread_subscriptions["subscribed"][room_id]), 3, thread_subscriptions
        )

    def test_limit_and_companion_backpagination(self) -> None:
        """
        Create 1 thread subscription, do a sync, create 4 more,
        then sync with a limit of 2 and fill in the gap
        using the companion /thread_subscriptions endpoint.
        """

        thread_root_ids: list[str] = []

        def make_subscription() -> None:
            thread_root_resp = self.helper.send(
                room_id, body="Some thread root", tok=user1_tok
            )
            thread_root_ids.append(thread_root_resp["event_id"])
            self._subscribe_to_thread(user1_id, room_id, thread_root_ids[-1])

        user1_id = self.register_user("user1", "pass")
        user1_tok = self.login(user1_id, "pass")
        room_id = self.helper.create_room_as(user1_id, tok=user1_tok)

        # get the baseline stream_id of the thread_subscriptions stream
        # before we write any data.
        # Required because the initial value differs between SQLite and Postgres.
        base = self.store.get_max_thread_subscriptions_stream_id()

        # Make our first subscription
        make_subscription()

        # Sync for the first time
        sync_body = {
            "lists": {},
            "extensions": {EXT_NAME: {"enabled": True, "limit": 2}},
        }

        sync_resp, first_sync_pos = self.do_sync(sync_body, tok=user1_tok)

        thread_subscriptions = sync_resp["extensions"][EXT_NAME]
        self.assertEqual(
            thread_subscriptions["subscribed"],
            {
                room_id: {
                    thread_root_ids[0]: {"automatic": False, "bump_stamp": base + 1},
                }
            },
        )

        # Get our pos for the next sync
        first_sync_pos = sync_resp["pos"]

        # Create 5 more thread subscriptions and subscribe to each
        for _ in range(5):
            make_subscription()

        # Now sync again. Our limit is 2,
        # so we should get the latest 2 subscriptions,
        # with a gap of 3 more subscriptions in the middle
        sync_resp, _pos = self.do_sync(sync_body, tok=user1_tok, since=first_sync_pos)

        thread_subscriptions = sync_resp["extensions"][EXT_NAME]
        self.assertEqual(
            thread_subscriptions["subscribed"],
            {
                room_id: {
                    thread_root_ids[4]: {"automatic": False, "bump_stamp": base + 5},
                    thread_root_ids[5]: {"automatic": False, "bump_stamp": base + 6},
                }
            },
        )
        # 1st backpagination: expecting a page with 2 subscriptions
        page, end_tok = self._do_backpaginate(
            from_tok=thread_subscriptions["prev_batch"],
            to_tok=first_sync_pos,
            limit=2,
            access_token=user1_tok,
        )
        self.assertIsNotNone(end_tok, "backpagination should continue")
        self.assertEqual(
            page["subscribed"],
            {
                room_id: {
                    thread_root_ids[2]: {"automatic": False, "bump_stamp": base + 3},
                    thread_root_ids[3]: {"automatic": False, "bump_stamp": base + 4},
                }
            },
        )

        # 2nd backpagination: expecting a page with only 1 subscription
        # and no other token for further backpagination
        assert end_tok is not None
        page, end_tok = self._do_backpaginate(
            from_tok=end_tok, to_tok=first_sync_pos, limit=2, access_token=user1_tok
        )
        self.assertIsNone(end_tok, "backpagination should have finished")
        self.assertEqual(
            page["subscribed"],
            {
                room_id: {
                    thread_root_ids[1]: {"automatic": False, "bump_stamp": base + 2},
                }
            },
        )

    def _do_backpaginate(
        self, *, from_tok: str, to_tok: str, limit: int, access_token: str
    ) -> tuple[JsonDict, str | None]:
        channel = self.make_request(
            "GET",
            "/_matrix/client/unstable/io.element.msc4308/thread_subscriptions"
            f"?from={from_tok}&to={to_tok}&limit={limit}&dir=b",
            access_token=access_token,
        )

        self.assertEqual(channel.code, HTTPStatus.OK, channel.json_body)
        body = channel.json_body
        return body, cast(str | None, body.get("end"))

    def _subscribe_to_thread(
        self, user_id: str, room_id: str, thread_root_id: str
    ) -> None:
        """
        Helper method to subscribe a user to a thread.
        """
        self.get_success(
            self.store.subscribe_user_to_thread(
                user_id=user_id,
                room_id=room_id,
                thread_root_event_id=thread_root_id,
                automatic_event_orderings=None,
            )
        )

    def _unsubscribe_from_thread(
        self, user_id: str, room_id: str, thread_root_id: str
    ) -> None:
        """
        Helper method to unsubscribe a user from a thread.
        """
        self.get_success(
            self.store.unsubscribe_user_from_thread(
                user_id=user_id,
                room_id=room_id,
                thread_root_event_id=thread_root_id,
            )
        )
