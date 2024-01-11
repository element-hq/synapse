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
from synapse.rest.health import HealthResource

from tests import unittest


class HealthCheckTests(unittest.HomeserverTestCase):
    def create_test_resource(self) -> HealthResource:
        # replace the JsonResource with a HealthResource.
        return HealthResource()

    def test_health(self) -> None:
        channel = self.make_request("GET", "/health", shorthand=False)

        self.assertEqual(channel.code, 200)
        self.assertEqual(channel.result["body"], b"OK")
