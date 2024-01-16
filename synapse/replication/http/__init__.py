#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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

from typing import TYPE_CHECKING

from synapse.http.server import JsonResource
from synapse.replication.http import (
    account_data,
    devices,
    federation,
    login,
    membership,
    presence,
    push,
    register,
    send_event,
    send_events,
    state,
    streams,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

REPLICATION_PREFIX = "/_synapse/replication"


class ReplicationRestResource(JsonResource):
    def __init__(self, hs: "HomeServer"):
        # We enable extracting jaeger contexts here as these are internal APIs.
        super().__init__(hs, canonical_json=False, extract_context=True)
        self.register_servlets(hs)

    def register_servlets(self, hs: "HomeServer") -> None:
        send_event.register_servlets(hs, self)
        send_events.register_servlets(hs, self)
        federation.register_servlets(hs, self)
        presence.register_servlets(hs, self)
        membership.register_servlets(hs, self)
        streams.register_servlets(hs, self)
        account_data.register_servlets(hs, self)
        push.register_servlets(hs, self)
        state.register_servlets(hs, self)

        # The following can't currently be instantiated on workers.
        if hs.config.worker.worker_app is None:
            login.register_servlets(hs, self)
            register.register_servlets(hs, self)
            devices.register_servlets(hs, self)
