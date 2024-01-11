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

from typing import TYPE_CHECKING

from synapse.http.server import HttpServer, JsonResource
from synapse.rest.key.v2.local_key_resource import LocalKey
from synapse.rest.key.v2.remote_key_resource import RemoteKey

if TYPE_CHECKING:
    from synapse.server import HomeServer


class KeyResource(JsonResource):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs, canonical_json=True)
        self.register_servlets(self, hs)

    @staticmethod
    def register_servlets(http_server: HttpServer, hs: "HomeServer") -> None:
        LocalKey(hs).register(http_server)
        RemoteKey(hs).register(http_server)
