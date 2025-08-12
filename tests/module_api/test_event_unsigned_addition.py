#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
from twisted.test.proto_helpers import MemoryReactor

from synapse.events import EventBase
from synapse.rest import admin, login, room
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util import Clock

from tests.unittest import HomeserverTestCase


class EventUnsignedAdditionTestCase(HomeserverTestCase):
    servlets = [
        room.register_servlets,
        admin.register_servlets,
        login.register_servlets,
    ]

    def prepare(
        self, reactor: MemoryReactor, clock: Clock, homeserver: HomeServer
    ) -> None:
        self._store = homeserver.get_datastores().main
        self._module_api = homeserver.get_module_api()
        self._account_data_mgr = self._module_api.account_data_manager

    def test_annotate_event(self) -> None:
        """Test that we can annotate an event when we request it from the
        server.
        """

        async def add_unsigned_event(event: EventBase) -> JsonDict:
            return {"test_key": event.event_id}

        self._module_api.register_add_extra_fields_to_unsigned_client_event_callbacks(
            add_field_to_unsigned_callback=add_unsigned_event
        )

        user_id = self.register_user("user", "password")
        token = self.login("user", "password")

        room_id = self.helper.create_room_as(user_id, tok=token)
        result = self.helper.send(room_id, "Hello!", tok=token)
        event_id = result["event_id"]

        event_json = self.helper.get_event(room_id, event_id, tok=token)
        self.assertEqual(event_json["unsigned"].get("test_key"), event_id)
