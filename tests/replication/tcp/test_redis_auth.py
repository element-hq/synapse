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
#

from typing import Any

from tests.replication._base import BaseMultiWorkerStreamTestCase


class RedisUsernameAuthTestCase(BaseMultiWorkerStreamTestCase):
    """Tests that Synapse authenticates to Redis with a username *and* password
    when both are configured, rather than with a bare password.
    """

    USERNAME = b"synapse-user"
    PASSWORD = b"correct-horse-battery-staple"

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config["redis"]["username"] = self.USERNAME.decode("utf-8")
        config["redis"]["password"] = self.PASSWORD.decode("utf-8")
        return config

    def test_auth_sent_with_username(self) -> None:
        """Both Redis connections the main process opens (one outbound, one
        subscriber) send `AUTH <username> <password>`.
        """
        # Let the AUTH replies flow back; nothing here needs virtual time to pass.
        self.reactor.advance(0)

        self.assertEqual(
            self._redis_server.auth_attempts,
            [(self.USERNAME, self.PASSWORD)] * 2,
        )

    def test_workers_authenticate_with_username_too(self) -> None:
        """A worker authenticates the same way as the main process, and both end
        up subscribed to the replication stream over those connections.
        """
        self.make_worker_hs("synapse.app.generic_worker")

        # Let the AUTH and SUBSCRIBE replies flow back.
        self.reactor.advance(0)

        self.assertEqual(
            self._redis_server.auth_attempts,
            [(self.USERNAME, self.PASSWORD)] * 4,
        )
        self.assertEqual(len(self._redis_server._subscribers_by_channel[b"test"]), 2)
