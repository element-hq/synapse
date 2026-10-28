#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
import attr
from parameterized import parameterized_class

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.api.constants import EventTypes, Membership, ReceiptTypes
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.handlers.server_announcements import SYNTHETIC_JOIN_EVENT_ID_PREFIX
from synapse.handlers.sliding_sync import StateValues
from synapse.rest.client import devices, login, receipts, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.rest.client.sliding_sync.test_sliding_sync import SlidingSyncBase


@parameterized_class(
    ("use_new_tables",),
    [
        (True,),
        (False,),
    ],
    class_name_func=lambda cls,
    num,
    params_dict: f"{cls.__name__}_{'new' if params_dict['use_new_tables'] else 'fallback'}",
)
class SlidingSyncServerAnnouncementsTestCase(SlidingSyncBase):
    """Tests that server announcement rooms are delivered over sliding sync to
    users who are not members of them, with a synthetic join for the syncing user.
    """

    servlets = [
        synapse.rest.admin.register_servlets,
        login.register_servlets,
        room.register_servlets,
        sync.register_servlets,
        devices.register_servlets,
        receipts.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        self.announcer = self.register_user("announcer", "pass")
        self.announcer_tok = self.login("announcer", "pass", device_id="ANNOUNCER")
        self.former = self.register_user("former", "pass")
        self.former_tok = self.login("former", "pass")
        self.bob = self.register_user("bob", "pass")
        self.bob_tok = self.login("bob", "pass")
        self.bob_other_tok = self.login("bob", "pass")

        self.config = hs.config.serverannouncements
        self.config.rooms = [
            ServerAnnouncementRoom(
                config_id="news",
                room_name="Server News",
                room_topic="News from the server",
                room_avatar_url=None,
                room_avatar_path=None,
                allowed_user_ids=[self.announcer, self.former],
            )
        ]
        self.handler = hs.get_server_announcements_handler()
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()
        self.get_success(self.handler.reconcile_rooms())
        room_id = self.get_success(self.store.get_server_announcement_room("news"))
        assert room_id is not None
        self.room_id = room_id

    def _sync_body(self, timeline_limit: int = 1) -> JsonDict:
        return {
            "lists": {
                "foo-list": {
                    "ranges": [[0, 99]],
                    "required_state": [
                        [EventTypes.Member, StateValues.ME],
                        [EventTypes.Name, ""],
                    ],
                    "timeline_limit": timeline_limit,
                }
            }
        }

    def _announce(self, body: str) -> str:
        return self.helper.send(self.room_id, body, tok=self.announcer_tok)["event_id"]

    def _list_room_ids(self, response: JsonDict) -> set[str]:
        return {
            room_id
            for op in response["lists"]["foo-list"].get("ops", [])
            for room_id in op.get("room_ids", [])
        }

    def _timeline_ids(self, room: JsonDict) -> list[str]:
        return [event["event_id"] for event in room["timeline"]]

    def _assert_not_delivered(self, response: JsonDict) -> None:
        self.assertNotIn(self.room_id, response.get("rooms", {}))
        self.assertNotIn(self.room_id, self._list_room_ids(response))

    def _assert_delivered(self, response: JsonDict, user_id: str) -> JsonDict:
        """Assert that the announcement room is in the list and the response,
        with a synthetic join for `user_id`, and return it.
        """
        self.assertIn(self.room_id, self._list_room_ids(response))
        room = response["rooms"][self.room_id]
        self._assert_synthetic_join(room, user_id)
        return room

    def _assert_synthetic_join(self, room: JsonDict, user_id: str) -> None:
        """Assert that the `required_state` of `room` holds a synthetic join for
        `user_id` as their only membership.
        """
        own_memberships = [
            event
            for event in room.get("required_state", [])
            if event["type"] == EventTypes.Member and event["state_key"] == user_id
        ]
        self.assertEqual(len(own_memberships), 1, room)
        membership = own_memberships[0]
        self.assertTrue(
            membership["event_id"].startswith(SYNTHETIC_JOIN_EVENT_ID_PREFIX),
            membership,
        )
        self.assertEqual(membership["content"], {"membership": Membership.JOIN})

    def _assert_no_own_membership_in_timeline(
        self, room: JsonDict, user_id: str
    ) -> None:
        """Assert that the timeline of `room` holds no membership event of
        `user_id`.
        """
        self.assertEqual(
            [
                event
                for event in room.get("timeline", [])
                if event["type"] == EventTypes.Member and event["state_key"] == user_id
            ],
            [],
        )

    def test_new_connection_delivers_announcements(self) -> None:
        """A new connection gets an announcement room with its most recent
        announcements and the state asked for.
        """
        event_ids = [self._announce(f"message {i}") for i in range(3)]

        response, _ = self.do_sync(self._sync_body(timeline_limit=2), tok=self.bob_tok)

        room = self._assert_delivered(response, self.bob)
        self.assertTrue(room["initial"])
        self.assertEqual(self._timeline_ids(room), event_ids[-2:])
        self.assertTrue(room["limited"])
        self.assertEqual(room["name"], "Server News")

        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(self.room_id)
        )
        self.assertIn(
            state_map[(EventTypes.Name, "")].event_id,
            {event["event_id"] for event in room["required_state"]},
        )

    def test_new_connection_without_announcement(self) -> None:
        """An announcement room without any announcement is left out of a new
        connection.
        """
        response, _ = self.do_sync(self._sync_body(), tok=self.bob_tok)

        self._assert_not_delivered(response)

    def test_existing_connection_delivers_new_announcement(self) -> None:
        """An existing connection gets an announcement room when a new
        announcement arrives.
        """
        response, pos = self.do_sync(self._sync_body(), tok=self.bob_tok)
        self._assert_not_delivered(response)

        event_id = self._announce("hello")

        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.bob_tok)
        room = self._assert_delivered(response, self.bob)
        self.assertTrue(room["initial"])
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_existing_connection_without_new_announcement(self) -> None:
        """An announcement room already sent on a connection is left out of the
        next response when nothing new was announced.
        """
        self._announce("hello")
        response, pos = self.do_sync(self._sync_body(), tok=self.bob_tok)
        self._assert_delivered(response, self.bob)

        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.bob_tok)
        self.assertNotIn(self.room_id, response.get("rooms", {}))

    def test_members_get_their_real_membership(self) -> None:
        """Members of an announcement room sync it as a regular room."""
        self._announce("hello")

        response, _ = self.do_sync(self._sync_body(), tok=self.announcer_tok)

        self.assertIn(self.room_id, self._list_room_ids(response))
        state_map = self.get_success(
            self.storage_controllers.state.get_current_state(self.room_id)
        )
        self._assertRequiredStateIncludes(
            response["rooms"][self.room_id]["required_state"],
            {
                state_map[(EventTypes.Member, self.announcer)],
                state_map[(EventTypes.Name, "")],
            },
            exact=True,
        )

    def test_self_left_member(self) -> None:
        """A member who left the room themselves gets later announcements as a
        non-member, with the synthetic join instead of their leave.
        """
        self.helper.leave(self.room_id, self.former, tok=self.former_tok)
        event_id = self._announce("hello")

        response, _ = self.do_sync(self._sync_body(), tok=self.former_tok)

        room = self._assert_delivered(response, self.former)
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_self_left_member_full_timeline(self) -> None:
        """A self-left member whose timeline reaches back to their leave gets the
        synthetic join, and not their leave in the timeline.
        """
        self.helper.leave(self.room_id, self.former, tok=self.former_tok)
        event_id = self._announce("hello")

        response, _ = self.do_sync(
            self._sync_body(timeline_limit=10), tok=self.former_tok
        )

        room = self._assert_delivered(response, self.former)
        self._assert_no_own_membership_in_timeline(room, self.former)
        self.assertEqual(self._timeline_ids(room)[-1], event_id)

    def test_self_left_member_on_existing_connection(self) -> None:
        """A member who leaves the room themselves while their connection is open
        gets later announcements as a non-member.
        """
        self._announce("hello")
        _, pos = self.do_sync(self._sync_body(), tok=self.former_tok)
        self.helper.leave(self.room_id, self.former, tok=self.former_tok)
        _, pos = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)
        event_id = self._announce("world")

        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)

        self.assertIn(self.room_id, self._list_room_ids(response))
        room = response["rooms"][self.room_id]
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_self_left_member_without_new_announcement(self) -> None:
        """A member who leaves the room themselves while their connection is open
        gets the synthetic join instead of their leave in the same sync, even
        when nothing new was announced.
        """
        self._announce("hello")
        _, pos = self.do_sync(self._sync_body(), tok=self.former_tok)
        self.helper.leave(self.room_id, self.former, tok=self.former_tok)

        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)

        room = self._assert_delivered(response, self.former)
        self._assert_no_own_membership_in_timeline(room, self.former)

    def test_self_left_member_before_any_announcement(self) -> None:
        """A member who leaves the room themselves while their connection is
        open, before anything was announced, gets the synthetic join instead of
        their leave in the same sync, then the first announcement.
        """
        _, pos = self.do_sync(self._sync_body(), tok=self.former_tok)
        self.helper.leave(self.room_id, self.former, tok=self.former_tok)

        response, pos = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)
        room = self._assert_delivered(response, self.former)
        self._assert_no_own_membership_in_timeline(room, self.former)

        event_id = self._announce("hello")
        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)

        self.assertIn(self.room_id, self._list_room_ids(response))
        room = response["rooms"][self.room_id]
        self.assertEqual(self._timeline_ids(room), [event_id])

    def _remove_former(self) -> None:
        """Drop `former` from the configured senders, which kicks them."""
        self.config.rooms = [
            attr.evolve(self.config.rooms[0], allowed_user_ids=[self.announcer])
        ]
        self.get_success(self.handler.reconcile_rooms())

    def test_removed_member(self) -> None:
        """A member removed from the configured senders gets later announcements
        as a non-member, with the synthetic join instead of their kick.
        """
        self._remove_former()
        event_id = self._announce("hello")

        response, _ = self.do_sync(self._sync_body(), tok=self.former_tok)

        room = self._assert_delivered(response, self.former)
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_removed_member_without_announcement(self) -> None:
        """A member removed from the configured senders before anything was
        announced does not get the room on a new connection, neither as joined
        nor with their kick.
        """
        self._remove_former()

        response, _ = self.do_sync(self._sync_body(), tok=self.former_tok)

        self._assert_not_delivered(response)

    def test_removed_member_room_changed_while_left_out(self) -> None:
        """A member removed from the configured senders before anything was
        announced gets the room's new name with the first announcement, when the
        name changed while the room was left out of their lists.
        """
        sync_body = self._sync_body(timeline_limit=5)
        _, pos = self.do_sync(sync_body, tok=self.former_tok)
        self._remove_former()
        _, pos = self.do_sync(sync_body, since=pos, tok=self.former_tok)
        self.config.rooms = [attr.evolve(self.config.rooms[0], room_name="New name")]
        self.get_success(self.handler.reconcile_rooms())
        response, pos = self.do_sync(sync_body, since=pos, tok=self.former_tok)
        self._assert_not_delivered(response)
        self._announce("hello")

        response, _ = self.do_sync(sync_body, since=pos, tok=self.former_tok)

        room = response["rooms"][self.room_id]
        self.assertEqual(room.get("name"), "New name")
        self.assertEqual(
            [
                event["content"]["name"]
                for event in room.get("required_state", [])
                if event["type"] == EventTypes.Name
            ],
            ["New name"],
        )

    def test_removed_member_existing_connection(self) -> None:
        """A member removed from the configured senders while their connection is
        open gets the synthetic join instead of their kick, even before anything
        was announced, then later announcements.
        """
        _, pos = self.do_sync(self._sync_body(), tok=self.former_tok)
        self._remove_former()

        response, pos = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)
        room = self._assert_delivered(response, self.former)
        self._assert_no_own_membership_in_timeline(room, self.former)

        event_id = self._announce("hello")
        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)

        self.assertIn(self.room_id, self._list_room_ids(response))
        room = response["rooms"][self.room_id]
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_removed_member_existing_connection_full_timeline(self) -> None:
        """A member removed from the configured senders while their connection is
        open, with a timeline reaching back to their kick, gets the synthetic
        join and not their kick in the timeline.
        """
        sync_body = self._sync_body(timeline_limit=10)
        _, pos = self.do_sync(sync_body, tok=self.former_tok)
        self._remove_former()

        response, _ = self.do_sync(sync_body, since=pos, tok=self.former_tok)

        room = self._assert_delivered(response, self.former)
        self._assert_no_own_membership_in_timeline(room, self.former)

    def test_removed_member_offline(self) -> None:
        """A member removed from the configured senders, with an announcement
        sent before their next sync, gets the announcement with the synthetic
        join instead of their kick in that sync.
        """
        _, pos = self.do_sync(self._sync_body(), tok=self.former_tok)
        self._remove_former()
        event_id = self._announce("hello")

        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.former_tok)

        room = self._assert_delivered(response, self.former)
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_removed_member_added_back(self) -> None:
        """A removed member added back to the configured senders while their
        connection is open gets their real join and the power levels that let
        them post again.
        """
        sync_body = self._sync_body()
        sync_body["lists"]["foo-list"]["required_state"].append(
            [EventTypes.PowerLevels, ""]
        )
        self._remove_former()
        _, pos = self.do_sync(sync_body, tok=self.former_tok)
        self.config.rooms = [
            attr.evolve(
                self.config.rooms[0], allowed_user_ids=[self.announcer, self.former]
            )
        ]
        self.get_success(self.handler.reconcile_rooms())

        response, _ = self.do_sync(sync_body, since=pos, tok=self.former_tok)

        state = {
            (event["type"], event["state_key"]): event
            for event in response["rooms"][self.room_id]["required_state"]
        }
        membership = state[(EventTypes.Member, self.former)]
        self.assertFalse(
            membership["event_id"].startswith(SYNTHETIC_JOIN_EVENT_ID_PREFIX)
        )
        self.assertEqual(membership["content"]["membership"], Membership.JOIN)
        self.assertEqual(
            state[(EventTypes.PowerLevels, "")]["content"]["users"][self.former], 50
        )

    def test_stays_in_list(self) -> None:
        """An announcement room already sent on a connection stays in the list
        when nothing new was announced.
        """
        self._announce("hello")
        _, pos = self.do_sync(self._sync_body(), tok=self.bob_tok)

        response, _ = self.do_sync(self._sync_body(), since=pos, tok=self.bob_tok)
        self.assertEqual(response["lists"]["foo-list"]["count"], 1)
        self.assertIn(self.room_id, self._list_room_ids(response))

    def test_ordered_like_other_rooms(self) -> None:
        """An announcement room is ordered among the user's own rooms by its
        latest activity.
        """
        own_room_id = self.helper.create_room_as(self.bob, tok=self.bob_tok)
        self._announce("hello")
        sync_body = self._sync_body()
        sync_body["lists"]["foo-list"]["ranges"] = [[0, 0]]

        response, pos = self.do_sync(sync_body, tok=self.bob_tok)
        self.assertEqual(
            response["lists"]["foo-list"]["ops"][0]["room_ids"], [self.room_id]
        )

        self.helper.send(own_room_id, "hi", tok=self.bob_tok)
        response, pos = self.do_sync(sync_body, since=pos, tok=self.bob_tok)
        self.assertEqual(
            response["lists"]["foo-list"]["ops"][0]["room_ids"], [own_room_id]
        )

        self._announce("again")
        response, _ = self.do_sync(sync_body, since=pos, tok=self.bob_tok)
        self.assertEqual(
            response["lists"]["foo-list"]["ops"][0]["room_ids"], [self.room_id]
        )

    def test_room_subscription(self) -> None:
        """Subscribing to an announcement room directly delivers it with the
        synthetic join.
        """
        event_id = self._announce("hello")
        sync_body = {
            "room_subscriptions": {
                self.room_id: {
                    "required_state": [[EventTypes.Member, StateValues.ME]],
                    "timeline_limit": 1,
                }
            }
        }

        response, _ = self.do_sync(sync_body, tok=self.bob_tok)

        room = response["rooms"][self.room_id]
        self._assert_synthetic_join(room, self.bob)
        self.assertEqual(self._timeline_ids(room), [event_id])

    def test_room_subscription_without_announcement(self) -> None:
        """Subscribing to an announcement room without any announcement delivers
        nothing.
        """
        sync_body = {
            "room_subscriptions": {
                self.room_id: {
                    "required_state": [[EventTypes.Member, StateValues.ME]],
                    "timeline_limit": 1,
                }
            }
        }

        response, _ = self.do_sync(sync_body, tok=self.bob_tok)

        self.assertNotIn(self.room_id, response.get("rooms", {}))

    def test_no_device_list_changes(self) -> None:
        """Device changes of members in an announcement room are not sent to
        non-members, while a user sharing a regular room with the member gets
        them.
        """
        carol = self.register_user("carol", "pass")
        carol_tok = self.login("carol", "pass")
        regular_room_id = self.helper.create_room_as(carol, tok=carol_tok)
        self.helper.join(regular_room_id, self.announcer, tok=self.announcer_tok)
        self._announce("hello")
        sync_body = {**self._sync_body(), "extensions": {"e2ee": {"enabled": True}}}
        _, carol_pos = self.do_sync(sync_body, tok=carol_tok)
        _, bob_pos = self.do_sync(sync_body, tok=self.bob_tok)

        channel = self.make_request(
            "PUT",
            "/devices/ANNOUNCER",
            {"display_name": "New Device Name"},
            access_token=self.announcer_tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

        carol_response, _ = self.do_sync(sync_body, since=carol_pos, tok=carol_tok)
        bob_response, _ = self.do_sync(sync_body, since=bob_pos, tok=self.bob_tok)

        self.assertIn(
            self.announcer,
            carol_response["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed", []),
        )
        self.assertNotIn(
            self.announcer,
            bob_response["extensions"]["e2ee"]
            .get("device_lists", {})
            .get("changed", []),
        )

    def _send_read_receipt(self, event_id: str, tok: str) -> None:
        channel = self.make_request(
            "POST",
            f"/rooms/{self.room_id}/receipt/{ReceiptTypes.READ}/{event_id}",
            {},
            access_token=tok,
        )
        self.assertEqual(channel.code, 200, channel.json_body)

    def _receipts_sync_body(self) -> JsonDict:
        sync_body = self._sync_body()
        sync_body["extensions"] = {"receipts": {"enabled": True}}
        return sync_body

    def _receipt_readers(self, response: JsonDict, event_id: str) -> set[str]:
        """Return the users with an `m.read` receipt on `event_id` in the
        announcement room of the receipts extension.
        """
        receipt = (
            response["extensions"]["receipts"].get("rooms", {}).get(self.room_id, {})
        )
        return set(
            receipt.get("content", {}).get(event_id, {}).get(ReceiptTypes.READ, {})
        )

    def test_non_member_receipt_on_new_connection_of_other_device(self) -> None:
        """A non-member's read receipt is sent to a new connection of their
        other devices.
        """
        event_id = self._announce("hello")
        self._send_read_receipt(event_id, self.bob_tok)

        response, _ = self.do_sync(self._receipts_sync_body(), tok=self.bob_other_tok)
        self.assertEqual(self._receipt_readers(response, event_id), {self.bob})

    def test_non_member_receipt_on_existing_connection_of_other_device(self) -> None:
        """A non-member's read receipt is sent to an existing connection of their
        other devices, even with no new announcement since.
        """
        event_id = self._announce("hello")
        _, pos = self.do_sync(self._receipts_sync_body(), tok=self.bob_other_tok)

        self._send_read_receipt(event_id, self.bob_tok)

        response, _ = self.do_sync(
            self._receipts_sync_body(), since=pos, tok=self.bob_other_tok
        )
        self.assertEqual(self._receipt_readers(response, event_id), {self.bob})
