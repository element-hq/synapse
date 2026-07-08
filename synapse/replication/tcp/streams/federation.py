#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017 Vector Creations Ltd
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
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import attr

from synapse.replication.tcp.streams._base import (
    Stream,
    Token,
    current_token_without_instance,
    make_http_update_function,
)
from synapse.types import JsonDict

if TYPE_CHECKING:
    from synapse.server import HomeServer


class FederationStream(Stream):
    """Data to be sent over federation. Only available when master has federation
    sending disabled.
    """

    @attr.s(slots=True, frozen=True, auto_attribs=True)
    class FederationStreamRow:
        type: str  # the type of data as defined in the BaseFederationRows
        data: JsonDict  # serialization of a federation.send_queue.BaseFederationRow

    NAME = "federation"
    ROW_TYPE = FederationStreamRow

    def __init__(self, hs: "HomeServer"):
        if hs.config.worker.worker_app is None:
            # master process: get updates from the FederationRemoteSendQueue.
            # (if the master is configured to send federation itself, federation_sender
            # will be a real FederationSender, which has stubs for current_token and
            # get_replication_rows.)
            federation_sender = hs.get_federation_sender()
            self.current_token_func = current_token_without_instance(
                federation_sender.get_current_token
            )
            update_function: Callable[
                [str, int, int, int], Awaitable[tuple[list[tuple[int, Any]], int, bool]]
            ] = federation_sender.get_replication_rows

        elif hs.should_send_federation():
            # federation sender: Query master process
            update_function = make_http_update_function(hs, self.NAME)
            self.current_token_func = self._stub_current_token

        else:
            # other worker: stub out the update function (we're not interested in
            # any updates so when we get a POSITION we do nothing)
            update_function = self._stub_update_function
            self.current_token_func = self._stub_current_token

        super().__init__(hs.get_instance_name(), update_function)

    def current_token(self, instance_name: str) -> Token:
        return self.current_token_func(instance_name)

    def minimal_local_current_token(self) -> Token:
        return self.current_token(self.local_instance_name)

    @staticmethod
    def _stub_current_token(instance_name: str) -> int:
        # dummy current-token method for use on workers
        return 0

    @staticmethod
    async def _stub_update_function(
        instance_name: str, from_token: int, upto_token: int, limit: int
    ) -> tuple[list, int, bool]:
        return [], upto_token, False
