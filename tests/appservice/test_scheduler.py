#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2015, 2016 OpenMarket Ltd
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
from typing import List, Optional, Sequence, Tuple, cast
from unittest.mock import AsyncMock, Mock

from typing_extensions import TypeAlias

from twisted.internet import defer
from twisted.test.proto_helpers import MemoryReactor

from synapse.appservice import (
    ApplicationService,
    ApplicationServiceState,
    TransactionOneTimeKeysCount,
    TransactionUnusedFallbackKeys,
)
from synapse.appservice.scheduler import (
    ApplicationServiceScheduler,
    _Recoverer,
    _TransactionController,
)
from synapse.events import EventBase
from synapse.logging.context import make_deferred_yieldable
from synapse.server import HomeServer
from synapse.types import DeviceListUpdates, JsonDict
from synapse.util import Clock

from tests import unittest

from ..utils import MockClock


class ApplicationServiceSchedulerTransactionCtrlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MockClock()
        self.store = Mock()
        self.as_api = Mock()
        self.recoverer = Mock()
        self.recoverer_fn = Mock(return_value=self.recoverer)
        self.txnctrl = _TransactionController(
            clock=cast(Clock, self.clock), store=self.store, as_api=self.as_api
        )
        self.txnctrl.RECOVERER_CLASS = self.recoverer_fn

    def test_single_service_up_txn_sent(self) -> None:
        # Test: The AS is up and the txn is successfully sent.
        service = Mock()
        events = [Mock(), Mock()]
        txn_id = "foobar"
        txn = Mock(id=txn_id, service=service, events=events)

        # mock methods
        self.store.get_appservice_state = AsyncMock(
            return_value=ApplicationServiceState.UP
        )
        txn.send = AsyncMock(return_value=True)
        txn.complete = AsyncMock(return_value=True)
        self.store.create_appservice_txn = AsyncMock(return_value=txn)

        # actual call
        self.successResultOf(defer.ensureDeferred(self.txnctrl.send(service, events)))

        self.store.create_appservice_txn.assert_called_once_with(
            service=service,
            events=events,
            ephemeral=[],
            to_device_messages=[],  # txn made and saved
            one_time_keys_count={},
            unused_fallback_keys={},
            device_list_summary=DeviceListUpdates(),
        )
        self.assertEqual(0, len(self.txnctrl.recoverers))  # no recoverer made
        txn.complete.assert_called_once_with(self.store)  # txn completed

    def test_single_service_down(self) -> None:
        # Test: The AS is down so it shouldn't push; Recoverers will do it.
        # It should still make a transaction though.
        service = Mock()
        events = [Mock(), Mock()]

        txn = Mock(id="idhere", service=service, events=events)
        self.store.get_appservice_state = AsyncMock(
            return_value=ApplicationServiceState.DOWN
        )
        self.store.create_appservice_txn = AsyncMock(return_value=txn)

        # actual call
        self.successResultOf(defer.ensureDeferred(self.txnctrl.send(service, events)))

        self.store.create_appservice_txn.assert_called_once_with(
            service=service,
            events=events,
            ephemeral=[],
            to_device_messages=[],  # txn made and saved
            one_time_keys_count={},
            unused_fallback_keys={},
            device_list_summary=DeviceListUpdates(),
        )
        self.assertEqual(0, txn.send.call_count)  # txn not sent though
        self.assertEqual(0, txn.complete.call_count)  # or completed

    def test_single_service_up_txn_not_sent(self) -> None:
        # Test: The AS is up and the txn is not sent. A Recoverer is made and
        # started.
        service = Mock()
        events = [Mock(), Mock()]
        txn_id = "foobar"
        txn = Mock(id=txn_id, service=service, events=events)

        # mock methods
        self.store.get_appservice_state = AsyncMock(
            return_value=ApplicationServiceState.UP
        )
        self.store.set_appservice_state = AsyncMock(return_value=True)
        txn.send = AsyncMock(return_value=False)  # fails to send
        self.store.create_appservice_txn = AsyncMock(return_value=txn)

        # actual call
        self.successResultOf(defer.ensureDeferred(self.txnctrl.send(service, events)))

        self.store.create_appservice_txn.assert_called_once_with(
            service=service,
            events=events,
            ephemeral=[],
            to_device_messages=[],
            one_time_keys_count={},
            unused_fallback_keys={},
            device_list_summary=DeviceListUpdates(),
        )
        self.assertEqual(1, self.recoverer_fn.call_count)  # recoverer made
        self.assertEqual(1, self.recoverer.recover.call_count)  # and invoked
        self.assertEqual(1, len(self.txnctrl.recoverers))  # and stored
        self.assertEqual(0, txn.complete.call_count)  # txn not completed
        self.store.set_appservice_state.assert_called_once_with(
            service,
            ApplicationServiceState.DOWN,  # service marked as down
        )


class ApplicationServiceSchedulerRecovererTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MockClock()
        self.as_api = Mock()
        self.store = Mock()
        self.service = Mock()
        self.callback = AsyncMock()
        self.recoverer = _Recoverer(
            clock=cast(Clock, self.clock),
            as_api=self.as_api,
            store=self.store,
            service=self.service,
            callback=self.callback,
        )

    def test_recover_single_txn(self) -> None:
        txn = Mock()
        # return one txn to send, then no more old txns
        txns = [txn, None]

        def take_txn(
            *args: object, **kwargs: object
        ) -> "defer.Deferred[Optional[Mock]]":
            return defer.succeed(txns.pop(0))

        self.store.get_oldest_unsent_txn = Mock(side_effect=take_txn)

        self.recoverer.recover()
        # shouldn't have called anything prior to waiting for exp backoff
        self.assertEqual(0, self.store.get_oldest_unsent_txn.call_count)
        txn.send = AsyncMock(return_value=True)
        txn.complete = AsyncMock(return_value=None)
        # wait for exp backoff
        self.clock.advance_time(2)
        self.assertEqual(1, txn.send.call_count)
        self.assertEqual(1, txn.complete.call_count)
        # 2 because it needs to get None to know there are no more txns
        self.assertEqual(2, self.store.get_oldest_unsent_txn.call_count)
        self.callback.assert_called_once_with(self.recoverer)
        self.assertEqual(self.recoverer.service, self.service)

    def test_recover_retry_txn(self) -> None:
        txn = Mock()
        txns = [txn, None]
        pop_txn = False

        def take_txn(
            *args: object, **kwargs: object
        ) -> "defer.Deferred[Optional[Mock]]":
            if pop_txn:
                return defer.succeed(txns.pop(0))
            else:
                return defer.succeed(txn)

        self.store.get_oldest_unsent_txn = Mock(side_effect=take_txn)

        self.recoverer.recover()
        self.assertEqual(0, self.store.get_oldest_unsent_txn.call_count)
        txn.send = AsyncMock(return_value=False)
        txn.complete = AsyncMock(return_value=None)
        self.clock.advance_time(2)
        self.assertEqual(1, txn.send.call_count)
        self.assertEqual(0, txn.complete.call_count)
        self.assertEqual(0, self.callback.call_count)
        self.clock.advance_time(4)
        self.assertEqual(2, txn.send.call_count)
        self.assertEqual(0, txn.complete.call_count)
        self.assertEqual(0, self.callback.call_count)
        self.clock.advance_time(8)
        self.assertEqual(3, txn.send.call_count)
        self.assertEqual(0, txn.complete.call_count)
        self.assertEqual(0, self.callback.call_count)
        txn.send = AsyncMock(return_value=True)  # successfully send the txn
        pop_txn = True  # returns the txn the first time, then no more.
        self.clock.advance_time(16)
        self.assertEqual(1, txn.send.call_count)  # new mock reset call count
        self.assertEqual(1, txn.complete.call_count)
        self.callback.assert_called_once_with(self.recoverer)


# Corresponds to synapse.appservice.scheduler._TransactionController.send
TxnCtrlArgs: TypeAlias = """
defer.Deferred[
    Tuple[
        ApplicationService,
        Sequence[EventBase],
        Optional[List[JsonDict]],
        Optional[List[JsonDict]],
        Optional[TransactionOneTimeKeysCount],
        Optional[TransactionUnusedFallbackKeys],
        Optional[DeviceListUpdates],
    ]
]
"""


class ApplicationServiceSchedulerQueuerTestCase(unittest.HomeserverTestCase):
    def prepare(self, reactor: "MemoryReactor", clock: Clock, hs: HomeServer) -> None:
        self.scheduler = ApplicationServiceScheduler(hs)
        self.txn_ctrl = Mock()
        self.txn_ctrl.send = AsyncMock()

        # Replace instantiated _TransactionController instances with our Mock
        self.scheduler.txn_ctrl = self.txn_ctrl
        self.scheduler.queuer.txn_ctrl = self.txn_ctrl

    def test_send_single_event_no_queue(self) -> None:
        # Expect the event to be sent immediately.
        service = Mock(id=4)
        event = Mock()
        self.scheduler.enqueue_for_appservice(service, events=[event])
        self.txn_ctrl.send.assert_called_once_with(
            service, [event], [], [], None, None, DeviceListUpdates()
        )

    def test_send_single_event_with_queue(self) -> None:
        d: TxnCtrlArgs = defer.Deferred()
        self.txn_ctrl.send = Mock(return_value=make_deferred_yieldable(d))
        service = Mock(id=4)
        event = Mock(event_id="first")
        event2 = Mock(event_id="second")
        event3 = Mock(event_id="third")
        # Send an event and don't resolve it just yet.
        self.scheduler.enqueue_for_appservice(service, events=[event])
        # Send more events: expect send() to NOT be called multiple times.
        # (call enqueue_for_appservice multiple times deliberately)
        self.scheduler.enqueue_for_appservice(service, events=[event2])
        self.scheduler.enqueue_for_appservice(service, events=[event3])
        self.txn_ctrl.send.assert_called_with(
            service, [event], [], [], None, None, DeviceListUpdates()
        )
        self.assertEqual(1, self.txn_ctrl.send.call_count)
        # Resolve the send event: expect the queued events to be sent
        d.callback(service)
        self.txn_ctrl.send.assert_called_with(
            service, [event2, event3], [], [], None, None, DeviceListUpdates()
        )
        self.assertEqual(2, self.txn_ctrl.send.call_count)

    def test_multiple_service_queues(self) -> None:
        # Tests that each service has its own queue, and that they don't block
        # on each other.
        srv1 = Mock(id=4)
        srv_1_defer: "defer.Deferred[EventBase]" = defer.Deferred()
        srv_1_event = Mock(event_id="srv1a")
        srv_1_event2 = Mock(event_id="srv1b")

        srv2 = Mock(id=6)
        srv_2_defer: "defer.Deferred[EventBase]" = defer.Deferred()
        srv_2_event = Mock(event_id="srv2a")
        srv_2_event2 = Mock(event_id="srv2b")

        send_return_list = [srv_1_defer, srv_2_defer]

        def do_send(*args: object, **kwargs: object) -> "defer.Deferred[EventBase]":
            return make_deferred_yieldable(send_return_list.pop(0))

        self.txn_ctrl.send = Mock(side_effect=do_send)

        # send events for different ASes and make sure they are sent
        self.scheduler.enqueue_for_appservice(srv1, events=[srv_1_event])
        self.scheduler.enqueue_for_appservice(srv1, events=[srv_1_event2])
        self.txn_ctrl.send.assert_called_with(
            srv1, [srv_1_event], [], [], None, None, DeviceListUpdates()
        )
        self.scheduler.enqueue_for_appservice(srv2, events=[srv_2_event])
        self.scheduler.enqueue_for_appservice(srv2, events=[srv_2_event2])
        self.txn_ctrl.send.assert_called_with(
            srv2, [srv_2_event], [], [], None, None, DeviceListUpdates()
        )

        # make sure callbacks for a service only send queued events for THAT
        # service
        srv_2_defer.callback(srv2)
        self.txn_ctrl.send.assert_called_with(
            srv2, [srv_2_event2], [], [], None, None, DeviceListUpdates()
        )
        self.assertEqual(3, self.txn_ctrl.send.call_count)

    def test_send_large_txns(self) -> None:
        srv_1_defer: "defer.Deferred[EventBase]" = defer.Deferred()
        srv_2_defer: "defer.Deferred[EventBase]" = defer.Deferred()
        send_return_list = [srv_1_defer, srv_2_defer]

        def do_send(*args: object, **kwargs: object) -> "defer.Deferred[EventBase]":
            return make_deferred_yieldable(send_return_list.pop(0))

        self.txn_ctrl.send = Mock(side_effect=do_send)

        service = Mock(id=4, name="service")
        event_list = [Mock(name="event%i" % (i + 1)) for i in range(200)]
        for event in event_list:
            self.scheduler.enqueue_for_appservice(service, [event], [])

        # Expect the first event to be sent immediately.
        self.txn_ctrl.send.assert_called_with(
            service, [event_list[0]], [], [], None, None, DeviceListUpdates()
        )
        srv_1_defer.callback(service)
        # Then send the next 100 events
        self.txn_ctrl.send.assert_called_with(
            service, event_list[1:101], [], [], None, None, DeviceListUpdates()
        )
        srv_2_defer.callback(service)
        # Then the final 99 events
        self.txn_ctrl.send.assert_called_with(
            service, event_list[101:], [], [], None, None, DeviceListUpdates()
        )
        self.assertEqual(3, self.txn_ctrl.send.call_count)

    def test_send_single_ephemeral_no_queue(self) -> None:
        # Expect the event to be sent immediately.
        service = Mock(id=4, name="service")
        event_list = [Mock(name="event")]
        self.scheduler.enqueue_for_appservice(service, ephemeral=event_list)
        self.txn_ctrl.send.assert_called_once_with(
            service, [], event_list, [], None, None, DeviceListUpdates()
        )

    def test_send_multiple_ephemeral_no_queue(self) -> None:
        # Expect the event to be sent immediately.
        service = Mock(id=4, name="service")
        event_list = [Mock(name="event1"), Mock(name="event2"), Mock(name="event3")]
        self.scheduler.enqueue_for_appservice(service, ephemeral=event_list)
        self.txn_ctrl.send.assert_called_once_with(
            service, [], event_list, [], None, None, DeviceListUpdates()
        )

    def test_send_single_ephemeral_with_queue(self) -> None:
        d: TxnCtrlArgs = defer.Deferred()
        self.txn_ctrl.send = Mock(return_value=make_deferred_yieldable(d))
        service = Mock(id=4)
        event_list_1 = [Mock(event_id="event1"), Mock(event_id="event2")]
        event_list_2 = [Mock(event_id="event3"), Mock(event_id="event4")]
        event_list_3 = [Mock(event_id="event5"), Mock(event_id="event6")]

        # Send an event and don't resolve it just yet.
        self.scheduler.enqueue_for_appservice(service, ephemeral=event_list_1)
        # Send more events: expect send() to NOT be called multiple times.
        self.scheduler.enqueue_for_appservice(service, ephemeral=event_list_2)
        self.scheduler.enqueue_for_appservice(service, ephemeral=event_list_3)
        self.txn_ctrl.send.assert_called_with(
            service, [], event_list_1, [], None, None, DeviceListUpdates()
        )
        self.assertEqual(1, self.txn_ctrl.send.call_count)
        # Resolve txn_ctrl.send
        d.callback(service)
        # Expect the queued events to be sent
        self.txn_ctrl.send.assert_called_with(
            service,
            [],
            event_list_2 + event_list_3,
            [],
            None,
            None,
            DeviceListUpdates(),
        )
        self.assertEqual(2, self.txn_ctrl.send.call_count)

    def test_send_large_txns_ephemeral(self) -> None:
        d: TxnCtrlArgs = defer.Deferred()
        self.txn_ctrl.send = Mock(return_value=make_deferred_yieldable(d))
        # Expect the event to be sent immediately.
        service = Mock(id=4, name="service")
        first_chunk = [Mock(name="event%i" % (i + 1)) for i in range(100)]
        second_chunk = [Mock(name="event%i" % (i + 101)) for i in range(50)]
        event_list = first_chunk + second_chunk
        self.scheduler.enqueue_for_appservice(service, ephemeral=event_list)
        self.txn_ctrl.send.assert_called_once_with(
            service, [], first_chunk, [], None, None, DeviceListUpdates()
        )
        d.callback(service)
        self.txn_ctrl.send.assert_called_with(
            service, [], second_chunk, [], None, None, DeviceListUpdates()
        )
        self.assertEqual(2, self.txn_ctrl.send.call_count)
