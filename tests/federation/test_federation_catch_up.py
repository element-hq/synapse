import sqlite3
from typing import Callable, Collection
from unittest import mock
from unittest.mock import Mock

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.events import EventBase
from synapse.federation.sender import (
    FederationSender,
    PerDestinationQueue,
    TransactionManager,
)
from synapse.federation.sender.per_destination_queue import _PreparedTransaction
from synapse.federation.units import Edu, Transaction
from synapse.replication.tcp.streams._base import StickyEventStreamPosition
from synapse.rest import admin
from synapse.rest.client import login, room
from synapse.server import HomeServer
from synapse.types import JsonDict, RoomID
from synapse.util.clock import Clock
from synapse.util.duration import Duration
from synapse.util.retryutils import NotRetryingDestination

from tests.test_utils import event_injection
from tests.unittest import FederatingHomeserverTestCase
from tests.utils import USE_POSTGRES_FOR_TESTS


class _FederationCatchUpTestCaseBase(FederatingHomeserverTestCase):
    """
    Scaffolding for a homeserver with federation sending enabled
    and a mocked-out federation transport, so that
    outbound transactions can be recorded (or made to fail).
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.federation_transport_client = Mock(spec=["send_transaction"])
        return self.setup_test_homeserver(
            federation_transport_client=self.federation_transport_client,
        )

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # whenever send_transaction is called, record the pdu data
        self.pdus: list[JsonDict] = []
        self.failed_pdus: list[JsonDict] = []
        self.is_online = True
        self.federation_transport_client.send_transaction.side_effect = (
            self.record_transaction
        )

        federation_sender = hs.get_federation_sender()
        assert isinstance(federation_sender, FederationSender)
        self.federation_sender = federation_sender

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["federation_sender_instances"] = None
        return config

    async def record_transaction(
        self, txn: Transaction, json_cb: Callable[[], JsonDict] | None
    ) -> JsonDict:
        if json_cb is None:
            # The tests seem to expect that this method raises in this situation.
            raise Exception("Blank json_cb")
        elif self.is_online:
            data = json_cb()
            self.pdus.extend(data["pdus"])
            return {}
        else:
            data = json_cb()
            self.failed_pdus.extend(data["pdus"])
            raise NotRetryingDestination(0, 24 * 60 * 60 * 1000, txn.destination)

    def get_destination_room(self, room: str, destination: str = "host2") -> dict:
        """
        Gets the destination_rooms entry for a (destination, room_id) pair.

        Args:
            room: room ID
            destination: what destination, default is "host2"

        Returns:
            Dictionary of { event_id: str, stream_ordering: int }
        """
        event_id, stream_ordering = self.get_success(
            self.hs.get_datastores().main.db_pool.execute(
                "test:get_destination_rooms",
                """
                SELECT event_id, stream_ordering
                    FROM destination_rooms dr
                    JOIN events USING (stream_ordering)
                    WHERE dr.destination = ? AND dr.room_id = ?
                """,
                destination,
                room,
            )
        )[0]
        return {"event_id": event_id, "stream_ordering": stream_ordering}

    def make_fake_destination_queue(
        self, destination: str = "host2"
    ) -> tuple[PerDestinationQueue, list[EventBase]]:
        """
        Makes a fake per-destination queue.
        """
        transaction_manager = TransactionManager(self.hs)
        per_dest_queue = PerDestinationQueue(self.hs, transaction_manager, destination)
        results_list = []

        async def fake_send(
            destination_tm: str,
            pending_pdus: list[EventBase],
            _pending_edus: list[Edu],
        ) -> None:
            assert destination == destination_tm
            results_list.extend(pending_pdus)

        transaction_manager.send_new_transaction = fake_send  # type: ignore[assignment]

        return per_dest_queue, results_list

    def run_transaction(
        self, per_dest_queue: PerDestinationQueue
    ) -> _PreparedTransaction | None:
        """
        Prepares and completes one transaction, returning it.

        Returns None if there was no transaction to be made.
        """

        async def run() -> _PreparedTransaction | None:
            transaction = await per_dest_queue._prepare_transaction()
            if transaction is not None:
                # As the transmission loop does once the transaction has been sent.
                await per_dest_queue._complete_transaction(transaction)
            return transaction

        return self.get_success(run())


class FederationCatchUpTestCases(_FederationCatchUpTestCaseBase):
    """
    Tests cases of catching up over federation.

    By default for test cases federation sending is disabled. This Test class has it
    re-enabled for the main process.
    """

    def test_catch_up_destination_rooms_tracking(self) -> None:
        """
        Tests that we populate the `destination_rooms` table as needed.
        """
        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room = self.helper.create_room_as("u1", tok=u1_token)

        self.get_success(
            event_injection.inject_member_event(self.hs, room, "@user:host2", "join")
        )

        event_id_1 = self.helper.send(room, "wombats!", tok=u1_token)["event_id"]

        row_1 = self.get_destination_room(room)

        event_id_2 = self.helper.send(room, "rabbits!", tok=u1_token)["event_id"]

        row_2 = self.get_destination_room(room)

        # check: events correctly registered in order
        self.assertEqual(row_1["event_id"], event_id_1)
        self.assertEqual(row_2["event_id"], event_id_2)
        self.assertEqual(row_1["stream_ordering"], row_2["stream_ordering"] - 1)

    def test_catch_up_last_successful_stream_ordering_tracking(self) -> None:
        """
        Tests that we populate the `destination_rooms` table as needed.
        """
        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room = self.helper.create_room_as("u1", tok=u1_token)

        # take the remote offline
        self.is_online = False

        self.get_success(
            event_injection.inject_member_event(self.hs, room, "@user:host2", "join")
        )

        self.helper.send(room, "wombats!", tok=u1_token)
        self.pump()

        lsso_1 = self.get_success(
            self.hs.get_datastores().main.get_destination_last_successful_stream_ordering(
                "host2"
            )
        )

        self.assertIsNone(
            lsso_1,
            "There should be no last successful stream ordering for an always-offline destination",
        )

        # bring the remote online
        self.is_online = True

        event_id_2 = self.helper.send(room, "rabbits!", tok=u1_token)["event_id"]

        lsso_2 = self.get_success(
            self.hs.get_datastores().main.get_destination_last_successful_stream_ordering(
                "host2"
            )
        )
        row_2 = self.get_destination_room(room)

        self.assertEqual(
            self.pdus[0]["content"]["body"],
            "rabbits!",
            "Test fault: didn't receive the right PDU",
        )
        self.assertEqual(
            row_2["event_id"],
            event_id_2,
            "Test fault: destination_rooms not updated correctly",
        )
        self.assertEqual(
            lsso_2,
            row_2["stream_ordering"],
            "Send succeeded but not marked as last_successful_stream_ordering",
        )

    def test_catch_up_from_blank_state(self) -> None:
        """
        Runs an overall test of federation catch-up from scratch.
        Further tests will focus on more narrow aspects and edge-cases, but I
        hope to provide an overall view with this test.
        """
        # bring the other server online
        self.is_online = True

        # let's make some events for the other server to receive
        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_1 = self.helper.create_room_as("u1", tok=u1_token)
        room_2 = self.helper.create_room_as("u1", tok=u1_token)

        # also critical to federate
        self.get_success(
            event_injection.inject_member_event(self.hs, room_1, "@user:host2", "join")
        )
        self.get_success(
            event_injection.inject_member_event(self.hs, room_2, "@user:host2", "join")
        )

        self.helper.send_state(
            room_1, event_type="m.room.topic", body={"topic": "wombat"}, tok=u1_token
        )

        # check: PDU received for topic event
        self.assertEqual(len(self.pdus), 1)
        self.assertEqual(self.pdus[0]["type"], "m.room.topic")

        # take the remote offline
        self.is_online = False

        # send another event
        self.helper.send(room_1, "hi user!", tok=u1_token)

        # check: things didn't go well since the remote is down
        self.assertEqual(len(self.failed_pdus), 1)
        self.assertEqual(self.failed_pdus[0]["content"]["body"], "hi user!")

        # let's delete the federation transmission queue
        # (this pretends we are starting up fresh.)
        self.assertFalse(
            self.federation_sender._per_destination_queues[
                "host2"
            ].transmission_loop_running
        )
        del self.federation_sender._per_destination_queues["host2"]

        # let's also clear any backoffs
        self.get_success(
            self.hs.get_datastores().main.set_destination_retry_timings(
                "host2", None, 0, 0
            )
        )

        # bring the remote online and clear the received pdu list
        self.is_online = True
        self.pdus = []

        # now we need to initiate a federation transaction somehow…
        # to do that, let's send another event (because it's simple to do)
        # (do it to another room otherwise the catch-up logic decides it doesn't
        # need to catch up room_1 — something I overlooked when first writing
        # this test)
        self.helper.send(room_2, "wombats!", tok=u1_token)

        # we should now have received both PDUs
        self.assertEqual(len(self.pdus), 2)
        self.assertEqual(self.pdus[0]["content"]["body"], "hi user!")
        self.assertEqual(self.pdus[1]["content"]["body"], "wombats!")

    def test_catch_up_loop(self) -> None:
        """
        Tests the behaviour of _catch_up_transmission_loop.
        """

        # ARRANGE:
        #  - a local user (u1)
        #  - 3 rooms which u1 is joined to (and remote user @user:host2 is
        #    joined to)
        #  - some events (1 to 5) in those rooms
        #      we have 'already sent' events 1 and 2 to host2
        per_dest_queue, sent_pdus = self.make_fake_destination_queue()

        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_1 = self.helper.create_room_as("u1", tok=u1_token)
        room_2 = self.helper.create_room_as("u1", tok=u1_token)
        room_3 = self.helper.create_room_as("u1", tok=u1_token)
        self.get_success(
            event_injection.inject_member_event(self.hs, room_1, "@user:host2", "join")
        )
        self.get_success(
            event_injection.inject_member_event(self.hs, room_2, "@user:host2", "join")
        )
        self.get_success(
            event_injection.inject_member_event(self.hs, room_3, "@user:host2", "join")
        )

        # create some events
        self.helper.send(room_1, "you hear me!!", tok=u1_token)
        event_id_2 = self.helper.send(room_2, "wombats!", tok=u1_token)["event_id"]
        self.helper.send(room_3, "Matrix!", tok=u1_token)
        event_id_4 = self.helper.send(room_2, "rabbits!", tok=u1_token)["event_id"]
        event_id_5 = self.helper.send(room_3, "Synapse!", tok=u1_token)["event_id"]

        # destination_rooms should already be populated, but let us pretend that we already
        # sent (successfully) up to and including event id 2
        event_2 = self.get_success(self.hs.get_datastores().main.get_event(event_id_2))

        # also fetch event 5 so we know its last_successful_stream_ordering later
        event_5 = self.get_success(self.hs.get_datastores().main.get_event(event_id_5))

        assert event_2.internal_metadata.stream_ordering is not None
        self.get_success(
            self.hs.get_datastores().main.set_destination_last_successful_stream_ordering(
                "host2", event_2.internal_metadata.stream_ordering
            )
        )

        # ACT
        self.get_success(per_dest_queue._catch_up_transmission_loop())

        # ASSERT, noticing in particular:
        # - event 3 not sent out, because event 5 replaces it
        # - order is least recent first, so event 5 comes after event 4
        # - catch-up is completed
        self.assertEqual(len(sent_pdus), 2)
        self.assertEqual(sent_pdus[0].event_id, event_id_4)
        self.assertEqual(sent_pdus[1].event_id, event_id_5)
        self.assertFalse(per_dest_queue._catching_up)
        self.assertEqual(
            per_dest_queue._last_successful_stream_ordering,
            event_5.internal_metadata.stream_ordering,
        )

    def test_catch_up_on_synapse_startup(self) -> None:
        """
        Tests the behaviour of get_catch_up_outstanding_destinations and
            _wake_destinations_needing_catchup.
        """

        # list of sorted server names (note that there are more servers than the batch
        # size used in get_catch_up_outstanding_destinations).
        server_names = ["server%02d" % number for number in range(42)] + ["zzzerver"]

        # ARRANGE:
        #  - a local user (u1)
        #  - a room which u1 is joined to (and remote users @user:serverXX are
        #    joined to)

        # mark the remotes as online
        self.is_online = True

        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_id = self.helper.create_room_as("u1", tok=u1_token)

        for server_name in server_names:
            self.get_success(
                event_injection.inject_member_event(
                    self.hs, room_id, "@user:%s" % server_name, "join"
                )
            )

        # create an event
        self.helper.send(room_id, "deary me!", tok=u1_token)

        # ASSERT:
        # - All servers are up to date so none should have outstanding catch-up
        outstanding_when_successful = self.get_success(
            self.hs.get_datastores().main.get_catch_up_outstanding_destinations(None)
        )
        self.assertEqual(outstanding_when_successful, [])

        # ACT:
        # - Make the remote servers unreachable
        self.is_online = False

        # - Mark zzzerver as being backed-off from
        now = self.clock.time_msec()
        self.get_success(
            self.hs.get_datastores().main.set_destination_retry_timings(
                "zzzerver",
                now,
                now,
                24 * 60 * 60 * 1000,  # retry in 1 day
            )
        )

        # - Send an event
        self.helper.send(room_id, "can anyone hear me?", tok=u1_token)

        # ASSERT (get_catch_up_outstanding_destinations):
        # - all remotes are outstanding
        # - they are returned in batches of 25, in order
        outstanding_1 = self.get_success(
            self.hs.get_datastores().main.get_catch_up_outstanding_destinations(None)
        )

        self.assertEqual(len(outstanding_1), 25)
        self.assertEqual(outstanding_1, server_names[0:25])

        outstanding_2 = self.get_success(
            self.hs.get_datastores().main.get_catch_up_outstanding_destinations(
                outstanding_1[-1]
            )
        )
        self.assertNotIn("zzzerver", outstanding_2)
        self.assertEqual(len(outstanding_2), 17)
        self.assertEqual(outstanding_2, server_names[25:-1])

        # ACT: call _wake_destinations_needing_catchup

        # patch wake_destination to just count the destinations instead
        woken = set()

        def wake_destination_track(destination: str) -> None:
            woken.add(destination)

        self.federation_sender.wake_destination = wake_destination_track  # type: ignore[method-assign]

        # We wait quite long so that all dests can be woken up, since there is a delay
        # between them.
        self.pump(by=5.0)

        # ASSERT (_wake_destinations_needing_catchup):
        # - all remotes are woken up, save for zzzerver
        self.assertNotIn("zzzerver", woken)
        # - all destinations are woken, potentially more than once, since the
        # wake up is called regularly and we don't ack in this test that a transaction
        # has been successfully sent.
        self.assertCountEqual(woken, set(server_names[:-1]))

    def test_not_latest_event(self) -> None:
        """Test that we send the latest event in the room even if its not ours."""

        per_dest_queue, sent_pdus = self.make_fake_destination_queue()

        # Make a room with a local user, and two servers. One will go offline
        # and one will send some events.
        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_1 = self.helper.create_room_as("u1", tok=u1_token)

        self.get_success(
            event_injection.inject_member_event(self.hs, room_1, "@user:host2", "join")
        )
        event_1 = self.get_success(
            event_injection.inject_member_event(self.hs, room_1, "@user:host3", "join")
        )

        # First we send something from the local server, so that we notice the
        # remote is down and go into catchup mode.
        self.helper.send(room_1, "you hear me!!", tok=u1_token)

        # Now simulate us receiving an event from the still online remote.
        event_2 = self.get_success(
            event_injection.inject_event(
                self.hs,
                type=EventTypes.Message,
                sender="@user:host3",
                room_id=room_1,
                content={"msgtype": "m.text", "body": "Hello"},
            )
        )

        assert event_1.internal_metadata.stream_ordering is not None
        self.get_success(
            self.hs.get_datastores().main.set_destination_last_successful_stream_ordering(
                "host2", event_1.internal_metadata.stream_ordering
            )
        )

        self.get_success(per_dest_queue._catch_up_transmission_loop())

        # We expect only the last message from the remote, event_2, to have been
        # sent, rather than the last *local* event that was sent.
        self.assertEqual(len(sent_pdus), 1)
        self.assertEqual(sent_pdus[0].event_id, event_2.event_id)
        self.assertFalse(per_dest_queue._catching_up)

    def test_catch_up_is_not_blocked_by_remote_event_in_partial_state_room(
        self,
    ) -> None:
        """Detects (part of?) https://github.com/matrix-org/synapse/issues/15220."""
        # ARRANGE:
        # - a local user (u1)
        # - a room which contains u1 and two remote users, @u2:host2 and @u3:other
        # - events in that room such that
        #   - history visibility is restricted
        #   - u1 sent message events e1 and e2
        #   - afterwards, u3 sent a remote event e3
        # - catchup to begin for host2; last successfully sent event was e1
        per_dest_queue, sent_pdus = self.make_fake_destination_queue()

        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room = self.helper.create_room_as("u1", tok=u1_token)
        self.helper.send_state(
            room_id=room,
            event_type="m.room.history_visibility",
            body={"history_visibility": "joined"},
            tok=u1_token,
        )
        self.get_success(
            event_injection.inject_member_event(self.hs, room, "@u2:host2", "join")
        )
        self.get_success(
            event_injection.inject_member_event(self.hs, room, "@u3:other", "join")
        )

        # create some events
        event_id_1 = self.helper.send(room, "hello", tok=u1_token)["event_id"]
        event_id_2 = self.helper.send(room, "world", tok=u1_token)["event_id"]
        # pretend that u3 changes their displayname
        event_id_3 = self.get_success(
            event_injection.inject_member_event(self.hs, room, "@u3:other", "join")
        ).event_id

        # destination_rooms should already be populated, but let us pretend that we already
        # sent (successfully) up to and including event id 1
        event_1 = self.get_success(self.hs.get_datastores().main.get_event(event_id_1))
        assert event_1.internal_metadata.stream_ordering is not None
        self.get_success(
            self.hs.get_datastores().main.set_destination_last_successful_stream_ordering(
                "host2", event_1.internal_metadata.stream_ordering
            )
        )

        # also fetch event 2 so we can compare its stream ordering to the sender's
        # last_successful_stream_ordering later
        event_2 = self.get_success(self.hs.get_datastores().main.get_event(event_id_2))

        # Mock event 3 as having partial state
        self.get_success(
            event_injection.mark_event_as_partial_state(self.hs, event_id_3, room)
        )

        # Fail the test if we block on full state for event 3.
        async def mock_await_full_state(event_ids: Collection[str]) -> None:
            if event_id_3 in event_ids:
                raise AssertionError("Tried to await full state for event_id_3")

        # ACT
        with mock.patch.object(
            self.hs.get_storage_controllers().state._partial_state_events_tracker,
            "await_full_state",
            mock_await_full_state,
        ):
            self.get_success(per_dest_queue._catch_up_transmission_loop())

        # ASSERT
        # We should have:
        # - not sent event 3: it's not ours, and the room is partial stated
        # - fallen back to sending event 2: it's the most recent event in the room
        #   we tried to send to host2
        # - completed catch-up
        self.assertEqual(len(sent_pdus), 1)
        self.assertEqual(sent_pdus[0].event_id, event_id_2)
        self.assertFalse(per_dest_queue._catching_up)
        self.assertEqual(
            per_dest_queue._last_successful_stream_ordering,
            event_2.internal_metadata.stream_ordering,
        )


class FederationStickyEventCatchUpTestCase(_FederationCatchUpTestCaseBase):
    """
    Tests for the catch-up of backlogged sticky events over federation.
    """

    if not USE_POSTGRES_FOR_TESTS and sqlite3.sqlite_version_info < (3, 40, 0):
        skip = f"SQLite version is too old to support sticky events: {sqlite3.sqlite_version_info} (See https://github.com/element-hq/synapse/issues/19428)"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["experimental_features"] = {"msc4354_enabled": True}
        return config

    def _send_sticky(self, room_id: str, body: str, tok: str) -> str:
        """
        Send a sticky event.
        """
        return self.helper.send_sticky_event(
            room_id,
            EventTypes.Message,
            duration=Duration(minutes=1),
            content={"body": body, "msgtype": "m.text"},
            tok=tok,
        )["event_id"]

    def _stream_ordering_for(self, event_id: str) -> int:
        """
        Get the `stream_ordering` for the given event.
        """
        event = self.get_success(self.hs.get_datastores().main.get_event(event_id))
        stream_ordering = event.internal_metadata.stream_ordering
        assert stream_ordering is not None
        return stream_ordering

    def _sticky_stream_id_for(self, event_id: str) -> int:
        """
        Get the `sticky_events` `stream_id` for the given event.
        """
        return self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_one_onecol(
                table="sticky_events",
                keyvalues={"event_id": event_id},
                retcol="stream_id",
                desc="test:get_sticky_stream_id",
            )
        )

    def _backlog_rows(self) -> list[tuple[str, str, int]]:
        """
        All rows of the `destination_room_sticky_events_backlog` table.
        """
        rows = self.get_success(
            self.hs.get_datastores().main.db_pool.simple_select_list(
                table="destination_room_sticky_events_backlog",
                keyvalues=None,
                retcols=("destination", "room_id", "sticky_events_stream_position"),
            )
        )
        return sorted(rows)

    def _assert_up_to_date_as_of(self, event_id: str) -> None:
        """
        Sanity-checks that `host2` was last successfully sent `event_id`, i.e. that
        catch-up will resume from there.
        """
        self.assertEqual(
            self.get_success(
                self.hs.get_datastores().main.get_destination_last_successful_stream_ordering(
                    "host2"
                )
            ),
            self._stream_ordering_for(event_id),
            "test fault: host2 is not up to date as of the expected event",
        )

    def test_catch_up_records_skipped_sticky_events_as_backlogged(self) -> None:
        """
        Tests that 'regular' federation catch-up marks the sticky events in the gaps
        that it skips.

        See the docstring on `mark_backlogged_sticky_events_after_catchup_transaction`
        for a diagrammatical explanation.
        """

        per_dest_queue, sent_pdus = self.make_fake_destination_queue()
        # The queue starts up in 'sticky event backlogged' mode, but we want our test to cover
        # the steady-state. So clear the backlogged flag and allow it to be set naturally.
        per_dest_queue._sticky_event_backlog_tracker._backlogged = False

        # Create a local user and 3 rooms
        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_1 = self.helper.create_room_as("u1", tok=u1_token)
        room_2 = self.helper.create_room_as("u1", tok=u1_token)
        room_3 = self.helper.create_room_as("u1", tok=u1_token)
        for room_id in (room_1, room_2, room_3):
            self.get_success(
                event_injection.inject_member_event(
                    self.hs, room_id, "@user:host2", "join"
                )
            )

        # Set up some events according to this sequence:
        #
        #         stream_ordering ------------------------------->
        # room 1    •                  •
        #        (message)        sticky_id_5
        # room 2      •          •
        #        event_id_2  event_id_4
        # room 3        •                  •
        #           sticky_id_3        event_id_6

        self.helper.send_messages(room_1, 1, tok=u1_token)
        (event_id_2,) = self.helper.send_messages(room_2, 1, tok=u1_token)
        # Let the federation sender act on the events above.
        self.reactor.advance(0)

        # Put host2 down, to prevent the federation sender from succeeding in sending the
        # next events out (and to trigger catch-up mode).
        self.is_online = False
        self._assert_up_to_date_as_of(event_id_2)

        # This sticky event will fall in the gap that the first catch-up transaction
        # skips over, so it must end up backlogged.
        sticky_id_3 = self._send_sticky(room_3, "sticky in the gap", u1_token)
        (event_id_4,) = self.helper.send_messages(room_2, 1, tok=u1_token)
        # This one is itself a forward extremity, so it will get sent as catch-up and
        # must _not_ be backlogged.
        sticky_id_5 = self._send_sticky(room_1, "sticky extremity", u1_token)
        (event_id_6,) = self.helper.send_messages(room_3, 1, tok=u1_token)
        # Advance for the federation sender to trigger on those newly-sent events
        self.reactor.advance(0)

        # Now trigger a catch-up loop
        self.get_success(per_dest_queue._catch_up_transmission_loop())

        # First sanity-check what got sent and what the state of the 'regular'
        # catch-up is.
        # Each room's latest local event was sent, oldest first.
        self.assertEqual(
            [pdu.event_id for pdu in sent_pdus],
            [event_id_4, sticky_id_5, event_id_6],
        )
        self.assertFalse(per_dest_queue._catching_up, "should have completed catch-up")
        self.assertEqual(
            per_dest_queue._last_successful_stream_ordering,
            self._stream_ordering_for(event_id_6),
        )

        # Check the state of the sticky event backlog
        # - `sticky_id_3` fell in the gap that got skipped over by the catch-up
        #   transaction for room 2 (as room 2 had the oldest forward extremity
        #   of all the rooms.)
        #   So room 3 is backlogged.
        # - `sticky_id_5` got sent as a forward extremity, so room 1 is _not_
        #   backlogged.
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_3, self._sticky_stream_id_for(sticky_id_3))],
        )
        self.assertTrue(
            per_dest_queue._sticky_event_backlog_tracker.is_backlogged,
            "the queue must notice the backlog that it has just recorded",
        )

    def test_backlogged_sticky_events_are_sent_in_dedicated_transactions(self) -> None:
        """
        Tests the dedicated transactions that send backlogged sticky events.
        """
        per_dest_queue, _sent_pdus = self.make_fake_destination_queue()

        # Create a local user and a room
        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_id = self.helper.create_room_as("u1", tok=u1_token)
        self.get_success(
            event_injection.inject_member_event(self.hs, room_id, "@user:host2", "join")
        )

        # Send whilst host2 is up, so this is where catch-up will resume from later on
        (already_sent_id,) = self.helper.send_messages(room_id, 1, tok=u1_token)
        # Trigger federation sender
        self.reactor.advance(0)

        # Put host2 down, to prevent the federation sender from succeeding in sending the
        # next events out (and to trigger catch-up mode).
        self.is_online = False
        self._assert_up_to_date_as_of(already_sent_id)

        sticky_id_1 = self._send_sticky(room_id, "sticky 1", u1_token)
        sticky_id_2 = self._send_sticky(room_id, "sticky 2", u1_token)
        # Send an event to be the room's forward extremity
        # This will make catch-up skip over the 2 sticky events so we can use those 2
        # to test sticky event backlog catch-up
        self.helper.send_messages(room_id, 1, tok=u1_token)
        # Trigger federation sender
        self.reactor.advance(0)

        # Do a 'regular' catch-up transaction.
        # This is also what records the sticky event backlog
        self.get_success(per_dest_queue._catch_up_transmission_loop())
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_id, self._sticky_stream_id_for(sticky_id_1))],
        )

        # Build a transaction. Since the main queue has nothing to send,
        # this is a sticky event backlog transaction.
        transaction = self.run_transaction(per_dest_queue)
        assert transaction is not None
        self.assertEqual(
            [pdu.event_id for pdu in transaction.pdus], [sticky_id_1, sticky_id_2]
        )
        self.assertEqual(transaction.edus, [])

        # The position should have moved to just past the last one.
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_id, self._sticky_stream_id_for(sticky_id_2) + 1)],
        )

        # Try to build a transaction, but get None as neither the main queue
        # nor the sticky event backlog should have anything to send
        self.assertIsNone(self.run_transaction(per_dest_queue))
        self.assertEqual(self._backlog_rows(), [])
        self.assertFalse(per_dest_queue._sticky_event_backlog_tracker.is_backlogged)

    def test_backlogged_sticky_events_are_drained_by_the_same_loop(self) -> None:
        """
        Tests that a sticky event backlog recorded by catch-up is drained by the same
        transmission loop, rather than sitting there until unrelated traffic to the
        destination happens to start a new one.
        """
        per_dest_queue, sent_pdus = self.make_fake_destination_queue()
        # The queue starts up in 'sticky event backlogged' mode, but we want our test to cover
        # the steady-state. So clear the backlogged flag and allow it to be set naturally.
        per_dest_queue._sticky_event_backlog_tracker._backlogged = False

        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_id = self.helper.create_room_as("u1", tok=u1_token)
        self.get_success(
            event_injection.inject_member_event(self.hs, room_id, "@user:host2", "join")
        )

        # Send an event whilst host2 is up, so this is where catch-up will resume from.
        (already_sent_id,) = self.helper.send_messages(room_id, 1, tok=u1_token)
        self.reactor.advance(0)

        # Put host2 down
        self.is_online = False
        self._assert_up_to_date_as_of(already_sent_id)

        sticky_id = self._send_sticky(room_id, "backlogged sticky", u1_token)
        # The room's latest event, so catch-up sends this one and skips over the sticky
        # event, leaving it backlogged.
        (latest_id,) = self.helper.send_messages(room_id, 1, tok=u1_token)
        # Trigger the federation sender to fail to send the events and then
        # to go into catch-up mode
        self.reactor.advance(0)

        # Trigger federation catch-up
        per_dest_queue.attempt_new_transaction()
        assert per_dest_queue.active_transmission_loop is not None
        self.get_success(per_dest_queue.active_transmission_loop)

        # Catch-up sent the room's latest event, then the backlog transaction sent the
        # sticky event that catch-up had skipped.
        self.assertEqual([pdu.event_id for pdu in sent_pdus], [latest_id, sticky_id])
        self.assertEqual(self._backlog_rows(), [])
        self.assertFalse(per_dest_queue._sticky_event_backlog_tracker.is_backlogged)

    def test_backlogged_sticky_events_do_not_delay_pending_pdus(self) -> None:
        """
        Tests that the normal federation transmission queue gets the opportunity
        to send main queue (normal) transactions in between sticky event backlog transactions.
        """
        per_dest_queue, _sent_pdus = self.make_fake_destination_queue()

        self.register_user("u1", "you the one")
        u1_token = self.login("u1", "you the one")
        room_id = self.helper.create_room_as("u1", tok=u1_token)
        self.get_success(
            event_injection.inject_member_event(self.hs, room_id, "@user:host2", "join")
        )

        # Sent whilst host2 is up, so this is where catch-up will resume from.
        (already_sent_id,) = self.helper.send_messages(room_id, 1, tok=u1_token)
        # Let the federation sender act on the events above.
        self.reactor.advance(0)
        self.is_online = False
        self._assert_up_to_date_as_of(already_sent_id)

        # Stands in for a PDU still awaiting real-time delivery; queued by hand below.
        (realtime_id,) = self.helper.send_messages(room_id, 1, tok=u1_token)
        sticky_id = self._send_sticky(room_id, "backlogged sticky", u1_token)
        # The room's latest event, so catch-up sends this one and skips over the
        # sticky event, leaving it backlogged.
        self.helper.send_messages(room_id, 1, tok=u1_token)
        # Let the federation sender act on the events above.
        self.reactor.advance(0)

        # Catching up is what records the backlog.
        self.get_success(per_dest_queue._catch_up_transmission_loop())
        self.assertEqual(
            self._backlog_rows(),
            [("host2", room_id, self._sticky_stream_id_for(sticky_id))],
        )

        realtime_event = self.get_success(
            self.hs.get_datastores().main.get_event(realtime_id)
        )

        # Append to `_pending_pdus` rather than calling `send_pdu`, because
        # `send_pdu` will trigger `attempt_new_transaction()`,
        # but we want to be in control here so we can inspect each transaction
        # individually.
        per_dest_queue._pending_pdus.append(realtime_event)

        # Transaction 1 (normal) sends the real-time queued PDU...
        transaction = self.run_transaction(per_dest_queue)
        assert transaction is not None
        self.assertEqual([pdu.event_id for pdu in transaction.pdus], [realtime_id])
        # (...and it is removed from the queue once the transaction completes)
        self.assertEqual(per_dest_queue._pending_pdus, [])

        # Transaction 2 sends the sticky event backlog
        transaction = self.run_transaction(per_dest_queue)
        assert transaction is not None
        self.assertEqual([pdu.event_id for pdu in transaction.pdus], [sticky_id])


class FederationStickyEventBacklogDisabledTestCase(_FederationCatchUpTestCaseBase):
    def test_no_backlog_transaction_when_msc4354_disabled(self) -> None:
        """
        Tests that when MSC4354 switched off, the sticky backlog is not serviced.
        """
        per_dest_queue, _sent_pdus = self.make_fake_destination_queue()

        async def must_not_be_called(
            destination: str, *, limit: int = 50
        ) -> tuple[RoomID, StickyEventStreamPosition, list[str]] | None:
            raise AssertionError(
                "Consulted the sticky event backlog despite MSC4354 being disabled"
            )

        with mock.patch.object(
            self.hs.get_datastores().main,
            "get_backlogged_sticky_events_for_destination",
            must_not_be_called,
        ):
            # Check twice to make sure it's not down to round-robin
            self.assertIsNone(self.run_transaction(per_dest_queue))
            self.assertIsNone(self.run_transaction(per_dest_queue))
