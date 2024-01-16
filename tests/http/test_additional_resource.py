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
from typing import Any

from twisted.web.server import Request

from synapse.http.additional_resource import AdditionalResource
from synapse.http.server import respond_with_json
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from tests.server import FakeSite, make_request
from tests.unittest import HomeserverTestCase


class _AsyncTestCustomEndpoint:
    def __init__(self, config: JsonDict, module_api: Any) -> None:
        pass

    async def handle_request(self, request: Request) -> None:
        assert isinstance(request, SynapseRequest)
        respond_with_json(request, 200, {"some_key": "some_value_async"})


class _SyncTestCustomEndpoint:
    def __init__(self, config: JsonDict, module_api: Any) -> None:
        pass

    async def handle_request(self, request: Request) -> None:
        assert isinstance(request, SynapseRequest)
        respond_with_json(request, 200, {"some_key": "some_value_sync"})


class AdditionalResourceTests(HomeserverTestCase):
    """Very basic tests that `AdditionalResource` works correctly with sync
    and async handlers.
    """

    def test_async(self) -> None:
        handler = _AsyncTestCustomEndpoint({}, None).handle_request
        resource = AdditionalResource(self.hs, handler)

        channel = make_request(
            self.reactor, FakeSite(resource, self.reactor), "GET", "/"
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"some_key": "some_value_async"})

    def test_sync(self) -> None:
        handler = _SyncTestCustomEndpoint({}, None).handle_request
        resource = AdditionalResource(self.hs, handler)

        channel = make_request(
            self.reactor, FakeSite(resource, self.reactor), "GET", "/"
        )

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.json_body, {"some_key": "some_value_sync"})
