from typing import Any, Literal
from unittest.mock import patch

from twisted.internet.testing import MemoryReactor

import synapse
from synapse.api.constants import EventTypes, RoomEncryptionAlgorithms
from synapse.events import EventBase
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.types import JsonDict, StateMap, UserID, create_requester
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config


class EncryptedByDefaultTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_encrypted_by_default_config_option_all(self) -> None:
        """Tests that invite-only and non-invite-only rooms have encryption enabled by
        default when the config option encryption_enabled_by_default_for_room_type is "all".
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create an invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=False, tok=user_token)

        # Check that the room has an encryption state event
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

        # Create a non invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=True, tok=user_token)

        # Check that the room has an encryption state event
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

    @override_config({"encryption_enabled_by_default_for_room_type": "invite"})
    def test_encrypted_by_default_config_option_invite(self) -> None:
        """Tests that only new, invite-only rooms have encryption enabled by default when
        the config option encryption_enabled_by_default_for_room_type is "invite".
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create an invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=False, tok=user_token)

        # Check that the room has an encryption state event
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

        # Create a non invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=True, tok=user_token)

        # Check that the room does not have an encryption state event
        self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
            expect_code=404,
        )

    @override_config({"encryption_enabled_by_default_for_room_type": "off"})
    def test_encrypted_by_default_config_option_off(self) -> None:
        """Tests that neither new invite-only nor non-invite-only rooms have encryption
        enabled by default when the config option
        encryption_enabled_by_default_for_room_type is "off".
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create an invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=False, tok=user_token)

        # Check that the room does not have an encryption state event
        self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
            expect_code=404,
        )

        # Create a non invite-only room as that user
        room_id = self.helper.create_room_as(user, is_public=True, tok=user_token)

        # Check that the room does not have an encryption state event
        self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
            expect_code=404,
        )

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_user_supplied_encryption_event_is_not_overwritten(self) -> None:
        """Tests that an m.room.encryption event supplied by the user in the
        initial state takes precedence over the one that would otherwise be forced
        by encryption_enabled_by_default_for_room_type, rather than a duplicate
        default event being sent.
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, supplying our own encryption event with a non-default
        # algorithm in the initial state.
        custom_content = {"algorithm": "some.custom.invalid.algorithm"}
        room_id = self.helper.create_room_as(
            user,
            is_public=False,
            tok=user_token,
            extra_content={
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": custom_content,
                    }
                ]
            },
        )

        # Check that the room's encryption event is the one we supplied, not the
        # default that the config option would otherwise force.
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, custom_content)

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_empty_encryption_event_does_not_bypass_forced_encryption(self) -> None:
        """Tests that a user cannot bypass encryption_enabled_by_default_for_room_type
        by supplying an empty m.room.encryption event in the initial state. Since
        such an event is not valid (it lacks the required `algorithm` key), the
        forced default must still be applied.
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, supplying an empty encryption event in the initial state.
        room_id = self.helper.create_room_as(
            user,
            is_public=False,
            tok=user_token,
            extra_content={
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": {},
                    }
                ]
            },
        )

        # Check that the forced default encryption was still applied on top of the
        # empty event, rather than the bypass succeeding.
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})

    @override_config({"encryption_enabled_by_default_for_room_type": "all"})
    def test_non_string_algorithm_does_not_bypass_forced_encryption(self) -> None:
        """Tests that a user cannot bypass encryption_enabled_by_default_for_room_type
        by supplying an m.room.encryption event whose `algorithm` is not a string.
        The forced default must still be applied.
        """
        # Create a user
        user = self.register_user("user", "pass")
        user_token = self.login(user, "pass")

        # Create a room, supplying an encryption event with a malformed
        # (non-string) algorithm in the initial state.
        room_id = self.helper.create_room_as(
            user,
            is_public=False,
            tok=user_token,
            extra_content={
                "initial_state": [
                    {
                        "type": EventTypes.RoomEncryption,
                        "state_key": "",
                        "content": {"algorithm": 42},
                    }
                ]
            },
        )

        # Check that the forced default encryption was still applied.
        event_content = self.helper.get_state(
            room_id=room_id,
            event_type=EventTypes.RoomEncryption,
            tok=user_token,
        )
        self.assertEqual(event_content, {"algorithm": RoomEncryptionAlgorithms.DEFAULT})


class RoomIDCollisionTestCase(unittest.HomeserverTestCase):
    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
    ]

    def test_colliding_v12_room_ids_are_retried(self) -> None:
        """In v12+ rooms the room ID is the reference hash of the
        create event, so two rooms whose create events have identical content
        collide on the same room ID. This happens when the same user creates
        several rooms at once (e.g. concurrent /createRoom requests with the
        same config within the same millisecond).

        Regression test: the collision must be retried transparently and both
        rooms created with distinct IDs.
        """
        handler = self.hs.get_room_creation_handler()
        user_id = self.register_user("alice", "pass")
        requester = create_requester(user_id)

        # Freeze the clock so both create events carry the same
        # `origin_server_ts`; with identical config this forces the two room
        # IDs to hash to the same value, reproducing the collision.
        with patch.object(self.hs.get_clock(), "time_msec", return_value=1234567890000):
            room_id1, _, _ = self.get_success(
                handler.create_room(requester, {"room_version": "12"}, ratelimit=False)
            )
            room_id2, _, _ = self.get_success(
                handler.create_room(requester, {"room_version": "12"}, ratelimit=False)
            )

        self.assertNotEqual(room_id1, room_id2)
        # v12 room IDs are content hashes with no domain component.
        self.assertNotIn(":", room_id1)
        self.assertNotIn(":", room_id2)


class CreateRoomPersistBatchesTestCase(unittest.HomeserverTestCase):
    """The initial events of a new room are persisted in as few batches as the
    room version allows, with the same state as when sent one by one."""

    servlets = [
        login.register_servlets,
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        room.register_servlets,
        sync.register_servlets,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.user_id = self.register_user("alice", "pass")
        self.tok = self.login("alice", "pass")
        self.get_success(
            hs.get_profile_handler().set_displayname(
                target_user=UserID.from_string(self.user_id),
                requester=create_requester(self.user_id),
                new_displayname="Alice",
            )
        )
        self.store = hs.get_datastores().main
        self.storage_controllers = hs.get_storage_controllers()

        # Record the event types of each call to persist events.
        self.batches: list[list[str]] = []
        persistence = self.storage_controllers.persistence
        assert persistence is not None
        persist_events = persistence.persist_events

        async def record_persist_events(
            events_and_contexts: Any, *args: Any, **kwargs: Any
        ) -> Any:
            self.batches.append([event.type for event, _ in events_and_contexts])
            return await persist_events(events_and_contexts, *args, **kwargs)

        patcher = patch.object(persistence, "persist_events", record_persist_events)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create_room(
        self,
        room_version: str,
        invite: list[str] | None = None,
        expect_code: int = 200,
    ) -> str:
        self.batches.clear()
        extra_content: JsonDict = {"name": "A room"}
        if invite:
            extra_content["invite"] = invite
        room_id = self.helper.create_room_as(
            self.user_id,
            tok=self.tok,
            room_version=room_version,
            extra_content=extra_content,
            expect_code=expect_code,
        )
        # Only set when the request succeeds.
        return room_id or ""

    def _check_room(self, room_id: str) -> None:
        state = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(
            set(state),
            {
                (EventTypes.Create, ""),
                (EventTypes.Member, self.user_id),
                (EventTypes.PowerLevels, ""),
                (EventTypes.JoinRules, ""),
                (EventTypes.RoomHistoryVisibility, ""),
                (EventTypes.Name, ""),
            },
        )

        join = state[(EventTypes.Member, self.user_id)]
        self.assertEqual(join.content, {"membership": "join", "displayname": "Alice"})
        create = state[(EventTypes.Create, "")]
        self.assertEqual(join.prev_event_ids(), [create.event_id])

        # The state after the create event is the create event alone, and its
        # state group follows on from an empty one.
        groups = self.get_success(
            self.storage_controllers.state.get_state_group_for_events(
                [create.event_id, join.event_id]
            )
        )
        create_group = groups[create.event_id]
        self.assertEqual(
            self.get_success(
                self.storage_controllers.state.get_state_ids_for_group(create_group)
            ),
            {(EventTypes.Create, ""): create.event_id},
        )
        prev_group = self.get_success(
            self.hs.get_datastores().state.db_pool.simple_select_one_onecol(
                table="state_group_edges",
                keyvalues={"state_group": create_group},
                retcol="prev_state_group",
            )
        )
        self.assertEqual(
            self.get_success(
                self.storage_controllers.state.get_state_ids_for_group(prev_group)
            ),
            {},
        )
        self.assertEqual(
            self.get_success(
                self.storage_controllers.state.get_state_ids_for_group(
                    groups[join.event_id]
                )
            ),
            {
                (EventTypes.Create, ""): create.event_id,
                (EventTypes.Member, self.user_id): join.event_id,
            },
        )

        # The creator is joined as far as the membership tables are concerned.
        self.assertIn(
            room_id, self.get_success(self.store.get_rooms_for_user(self.user_id))
        )

    def test_one_batch(self) -> None:
        """A room is persisted in one batch."""
        for room_version in ("10", "12"):
            room_id = self._create_room(room_version)
            self.assertEqual(
                self.batches,
                [
                    [
                        EventTypes.Create,
                        EventTypes.Member,
                        EventTypes.PowerLevels,
                        EventTypes.JoinRules,
                        EventTypes.RoomHistoryVisibility,
                        EventTypes.Name,
                    ]
                ],
            )
            self._check_room(room_id)

    def test_hashed_prev_events(self) -> None:
        """In v1 rooms the create event and the join are persisted on their own,
        as the builder reads their hashes from the database."""
        room_id = self._create_room("1")
        self.assertEqual(
            self.batches,
            [
                [EventTypes.Create],
                [EventTypes.Member],
                [
                    EventTypes.PowerLevels,
                    EventTypes.JoinRules,
                    EventTypes.RoomHistoryVisibility,
                    EventTypes.Name,
                ],
            ],
        )
        self._check_room(room_id)

    def test_aliases_in_initial_state(self) -> None:
        """An `m.room.aliases` event in the initial state needs the creator's
        join in the database, so the create event and the join go first."""
        self.batches.clear()
        self.helper.create_room_as(
            self.user_id,
            tok=self.tok,
            room_version="10",
            extra_content={
                "initial_state": [
                    {"type": EventTypes.Aliases, "state_key": "test", "content": {}}
                ]
            },
        )
        self.assertEqual(self.batches[:2], [[EventTypes.Create], [EventTypes.Member]])
        self.assertIn(EventTypes.Aliases, self.batches[2])

    def test_check_event_allowed_callback(self) -> None:
        """With a `check_event_allowed` callback, the create event and the join
        are persisted on their own. A callback that rewrites them leaves no
        extra state groups behind."""

        async def add_field(
            event: EventBase, state: StateMap[EventBase]
        ) -> tuple[bool, JsonDict | None]:
            if event.type not in (EventTypes.Create, EventTypes.Member):
                return True, None
            d = event.get_dict()
            d["content"] = {**event.content, "rewritten": True}
            return True, d

        self.hs.get_module_api_callbacks().third_party_event_rules._check_event_allowed_callbacks = [
            add_field
        ]

        for room_version in ("10", "12"):
            room_id = self._create_room(room_version)
            self.assertEqual(
                self.batches,
                [
                    [EventTypes.Create],
                    [EventTypes.Member],
                    [
                        EventTypes.PowerLevels,
                        EventTypes.JoinRules,
                        EventTypes.RoomHistoryVisibility,
                        EventTypes.Name,
                    ],
                ],
            )
            state = self.get_success(
                self.storage_controllers.state.get_current_state(room_id)
            )
            self.assertTrue(state[(EventTypes.Create, "")].content["rewritten"])
            self.assertTrue(
                state[(EventTypes.Member, self.user_id)].content["rewritten"]
            )

            # One empty group, then one per state event.
            groups = self.get_success(
                self.hs.get_datastores().state.db_pool.simple_select_onecol(
                    table="state_groups",
                    keyvalues={"room_id": room_id},
                    retcol="id",
                )
            )
            self.assertEqual(len(groups), len(state) + 1)

    def test_no_state_group_reads(self) -> None:
        """Creating a room reads no state groups from the database: the batch
        knows the state before each event and after the last one."""
        state_store = self.hs.get_datastores().state
        reads: list[object] = []
        orig = state_store._get_state_groups_from_groups

        async def record(*args: Any, **kwargs: Any) -> Any:
            reads.append(args)
            return await orig(*args, **kwargs)

        for room_version in ("10", "12"):
            with patch.object(state_store, "_get_state_groups_from_groups", record):
                self._create_room(room_version)
            self.assertEqual(reads, [])

    def _invite_event(self, room_id: str, invitee: str) -> EventBase:
        state = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        invite = state[(EventTypes.Member, invitee)]
        self.assertEqual(invite.membership, "invite")
        return invite

    def test_local_invites(self) -> None:
        """Invites to local users are persisted in the batch, with stripped
        state taken from the batch."""
        bob = self.register_user("bob", "pass")
        carol = self.register_user("carol", "pass")

        room_id = self._create_room("12", invite=[bob, carol])
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(
            self.batches[0][-3:],
            [EventTypes.Name, EventTypes.Member, EventTypes.Member],
        )

        invite = self._invite_event(room_id, bob)
        stripped = {
            (e["type"], e["state_key"]) for e in invite.unsigned["invite_room_state"]
        }
        self.assertIn((EventTypes.Create, ""), stripped)
        self.assertIn((EventTypes.JoinRules, ""), stripped)
        self.assertIn((EventTypes.Name, ""), stripped)
        self.assertIn((EventTypes.Member, self.user_id), stripped)
        self._invite_event(room_id, carol)

        # Bob sees the invite, with the stripped state, in his sync.
        bob_tok = self.login("bob", "pass")
        channel = self.make_request("GET", "/sync", access_token=bob_tok)
        self.assertEqual(channel.code, 200, channel.json_body)
        invited = channel.json_body["rooms"]["invite"][room_id]
        self.assertIn(
            EventTypes.Name,
            {e["type"] for e in invited["invite_state"]["events"]},
        )

    def test_user_may_invite_callback(self) -> None:
        """With a `user_may_invite` callback, invites are sent on their own
        after the room is created, so the module sees the room."""
        bob = self.register_user("bob", "pass")
        seen_create: list[bool] = []

        async def user_may_invite(
            inviter: str, invitee: str, room_id: str
        ) -> Literal["NOT_SPAM"]:
            create = await self.store.get_create_event_for_room(room_id)
            seen_create.append(create is not None)
            return "NOT_SPAM"

        self.hs.get_module_api_callbacks().spam_checker._user_may_invite_callbacks = [
            user_may_invite
        ]

        room_id = self._create_room("12", invite=[bob])
        self.assertEqual(len(self.batches), 2)
        self.assertEqual(self.batches[1], [EventTypes.Member])
        self.assertEqual(seen_create, [True])
        self._invite_event(room_id, bob)

    @override_config({"block_non_admin_invites": True})
    def test_refused_invite(self) -> None:
        """An invite that would be refused is sent on its own once the room
        exists, and fails the request as before."""
        bob = self.register_user("bob", "pass")
        self._create_room("12", invite=[bob], expect_code=403)

        # The room was created, without the invite.
        self.assertEqual(len(self.batches), 1)
        self.assertNotIn(EventTypes.Member, self.batches[0][2:])

    def test_invites_keep_ops_in_trusted_private_chat(self) -> None:
        """Batched invitees still get ops from the `trusted_private_chat`
        preset in rooms without MSC4289 creators."""
        bob = self.register_user("bob", "pass")
        room_id = self.helper.create_room_as(
            self.user_id,
            tok=self.tok,
            room_version="11",
            extra_content={"preset": "trusted_private_chat", "invite": [bob]},
        )
        state = self.get_success(
            self.storage_controllers.state.get_current_state(room_id)
        )
        self.assertEqual(state[(EventTypes.PowerLevels, "")].content["users"][bob], 100)
        self._invite_event(room_id, bob)

    def test_invite_failing_auth(self) -> None:
        """An invite that fails the auth rules is left out of the batch, so the
        room is still created before the request fails, as before."""
        bob = self.register_user("bob", "pass")
        for invite, extra in (
            # Alice is already in the room.
            ([self.user_id], {}),
            # Alice may not invite.
            ([bob], {"power_level_content_override": {"invite": 101}}),
        ):
            self.batches.clear()
            channel = self.make_request(
                "POST",
                "/createRoom",
                {"room_version": "10", "invite": invite, **extra},
                access_token=self.tok,
            )
            self.assertEqual(channel.code, 403, channel.json_body)
            self.assertEqual(len(self.batches), 1)
            self.assertNotIn(EventTypes.Member, self.batches[0][2:])

        rooms = self.get_success(self.store.get_rooms_for_user(self.user_id))
        self.assertEqual(len(rooms), 2)

    def test_duplicate_invites(self) -> None:
        """Inviting the same user twice sends one invite."""
        bob = self.register_user("bob", "pass")
        room_id = self._create_room("12", invite=[bob, bob])
        self.assertEqual(len(self.batches), 1)
        self.assertEqual(self.batches[0].count(EventTypes.Member), 2)
        self._invite_event(room_id, bob)
