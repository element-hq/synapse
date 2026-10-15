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
"""Run federation sender tests with Rust building the transactions, or at
`queues`, running the queues too.

The tests mock `TransportLayerClient.send_transaction`, which the Rust sender
does not call. `RustTransactionsMixin` puts a stand-in for the Rust sender in
its place: Rust builds each transaction, the mock is called with it as it
would be with a Python `Transaction`, and Rust reads the mock's response. The
HTTP half of the Rust sender is tested in `tests.http.test_rust_transactions`.
"""

import time
from typing import TYPE_CHECKING, Any, Callable, Sequence

from twisted.internet.testing import MemoryReactor, MemoryReactorClock

from synapse.events import EventBase
from synapse.federation.sender import FederationSender
from synapse.server import HomeServer
from synapse.synapse_rust.federation import (
    RustDestinationQueues,
    RustTransaction,
    RustTransactionManager,
)
from synapse.types import JsonDict
from synapse.util.clock import Clock

from tests.utils import RUST_FED_STRICT_FOR_TESTS

if TYPE_CHECKING:
    from unittest.mock import Mock

    from tests.unittest import HomeserverTestCase

    _Base = HomeserverTestCase
else:
    _Base = object


class _MockedRustTransactions:
    """Stands in for a `RustTransactionManager`, sending through the test's
    mocked `send_transaction`."""

    def __init__(self, rust: RustTransactionManager, transport: "Mock") -> None:
        self._rust = rust
        self._transport = transport

    async def send_transaction(
        self,
        destination: str,
        pdus: Sequence[EventBase],
        edus: Sequence[JsonDict],
        *,
        extra_headers: Sequence[tuple[str, str]] = (),
        want_acknowledged: bool = False,
        check: Callable[[RustTransaction], None] | None = None,
    ) -> list[str] | None:
        transaction = self._rust.build_transaction(destination, pdus, edus)
        if check is not None:
            check(transaction)

        response = await self._transport.send_transaction(
            transaction, transaction.get_dict
        )
        if not isinstance(response, dict):
            response = {}

        acknowledged = self._rust.acknowledged_pdus(transaction, response)
        return acknowledged if want_acknowledged else None


class RustTransactionsMixin(_Base):
    """Run a test case at `rust_federation_sending: transactions`. The test
    case must mock the federation transport client as
    `self.federation_transport_client`."""

    federation_transport_client: "Mock"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_federation_sending"] = (
            "transactions"
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        sender = hs.get_federation_sender()
        assert isinstance(sender, FederationSender)
        manager = sender._transaction_manager
        assert manager._rust is not None
        manager._rust_strict = RUST_FED_STRICT_FOR_TESTS
        manager._rust = _MockedRustTransactions(  # type: ignore[assignment]
            manager._rust, self.federation_transport_client
        )


def wait_for_rust_queues(
    reactor: MemoryReactorClock, queues: RustDestinationQueues
) -> None:
    """Advance the reactor by nothing until every Rust transmission loop has
    finished or waits on something that needs time to pass.

    The loops run on tokio, and the reactor does not wait for them. A loop is
    done for now when it is idle and the reactor has nothing due.
    """
    for i in range(5000):
        reactor.advance(0)
        due = any(
            call.getTime() <= reactor.seconds() for call in reactor.getDelayedCalls()
        )
        if queues.idle() and not due:
            return
        time.sleep(0 if i < 10 else 0.001)
    raise AssertionError("the Rust federation queues did not settle")


class RustQueuesMixin(_Base):
    """Run a test case at `rust_federation_sending: queues`. The test case
    must mock the federation transport client as
    `self.federation_transport_client`.

    The Rust loops send through the mocked `send_transaction`, as the Python
    loop does. Every `reactor.advance` waits for the loops to settle, so the
    test's checks after one see what the loops did.
    """

    federation_transport_client: "Mock"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_federation_sending"] = (
            "queues"
        )
        return config

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        super().prepare(reactor, clock, hs)

        sender = hs.get_federation_sender()
        assert isinstance(sender, FederationSender)
        queues = sender._rust_queues
        assert queues is not None
        transport = self.federation_transport_client

        async def send(transaction: RustTransaction) -> Any:
            return await transport.send_transaction(transaction, transaction.get_dict)

        queues.set_send_for_test(send)

        # Catch-up still sends through the transaction manager.
        manager = sender._transaction_manager
        assert manager._rust is not None
        manager._rust_strict = RUST_FED_STRICT_FOR_TESTS
        manager._rust = _MockedRustTransactions(  # type: ignore[assignment]
            manager._rust, transport
        )

        reactor = self.reactor
        advance = reactor.advance
        waiting = False

        def advance_and_wait(amount: float) -> None:
            nonlocal waiting
            advance(amount)
            if waiting:
                return
            waiting = True
            try:
                wait_for_rust_queues(reactor, queues)
            finally:
                waiting = False

        reactor.advance = advance_and_wait  # type: ignore[method-assign]
