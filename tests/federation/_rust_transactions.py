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
"""Run federation sender tests with Rust building the transactions.

The tests mock `TransportLayerClient.send_transaction`, which the Rust sender
does not call. `RustTransactionsMixin` puts a stand-in for the Rust sender in
its place: Rust builds each transaction, the mock is called with it as it
would be with a Python `Transaction`, and Rust reads the mock's response. The
HTTP half of the Rust sender is tested in `tests.http.test_rust_transactions`.
"""

from typing import TYPE_CHECKING, Callable, Sequence

from twisted.internet.testing import MemoryReactor

from synapse.events import EventBase
from synapse.federation.sender import FederationSender
from synapse.server import HomeServer
from synapse.synapse_rust.federation import RustTransaction, RustTransactionManager
from synapse.types import JsonDict
from synapse.util.clock import Clock

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
        manager._rust = _MockedRustTransactions(  # type: ignore[assignment]
            manager._rust, self.federation_transport_client
        )
