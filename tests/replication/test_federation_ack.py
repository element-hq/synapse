#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from unittest import mock

from twisted.internet.testing import MemoryReactor

from synapse.app.generic_worker import GenericWorkerServer
from synapse.replication.tcp.commands import FederationAckCommand
from synapse.replication.tcp.protocol import IReplicationConnection
from synapse.replication.tcp.streams.federation import FederationStream
from synapse.server import HomeServer
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class FederationAckTestCase(HomeserverTestCase):
    def default_config(self) -> dict:
        config = super().default_config()
        config["worker_app"] = "synapse.app.generic_worker"
        config["worker_name"] = "federation_sender1"
        config["federation_sender_instances"] = ["federation_sender1"]
        config["instance_map"] = {"main": {"host": "127.0.0.1", "port": 0}}
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        return self.setup_test_homeserver(homeserver_to_use=GenericWorkerServer)

    def test_federation_ack_sent(self) -> None:
        """A FEDERATION_ACK should be sent back after each RDATA federation

        This test checks that the federation sender is correctly sending back
        FEDERATION_ACK messages. The test works by spinning up a federation_sender
        worker server, and then fishing out its ReplicationCommandHandler. We wire
        the RCH up to a mock connection (so that we can observe the command being sent)
        and then poke in an RDATA row.

        XXX: it might be nice to do this by pretending to be a synapse master worker
        (or a redis server), and having the worker connect to us via a mocked-up TCP
        transport, rather than assuming that the implementation has a
        ReplicationCommandHandler.
        """
        rch = self.hs.get_replication_command_handler()

        # wire up the ReplicationCommandHandler to a mock connection, which needs
        # to implement IReplicationConnection. (Note that Mock doesn't understand
        # interfaces, but casing an interface to a list gives the attributes.)
        mock_connection = mock.Mock(spec=list(IReplicationConnection))
        rch.new_connection(mock_connection)

        # tell it it received an RDATA row
        self.get_success(
            rch.on_rdata(
                "federation",
                "master",
                token=10,
                rows=[
                    FederationStream.FederationStreamRow(
                        type="x", data={"test": [1, 2, 3]}
                    )
                ],
            )
        )

        # now check that the FEDERATION_ACK was sent
        mock_connection.send_command.assert_called_once()
        cmd = mock_connection.send_command.call_args[0][0]
        assert isinstance(cmd, FederationAckCommand)
        self.assertEqual(cmd.token, 10)
