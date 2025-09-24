#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

"""Tests REST events for /rtc/endpoints path."""


from synapse.rest import admin
from synapse.rest.client import login, matrixrtc, register,room

from tests import unittest

PATH_PREFIX = "/_matrix/client/unstable/org.matrix.msc4143"
RTC_ENDPOINT = {"type": "focusA", "required_field": "theField"}

class MatrixRtcTestCase(unittest.HomeserverTestCase):
    """Tests /rtc/endpoints REST API."""

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        matrixrtc.register_servlets
    ]

    @unittest.override_config({"matrix_rtc": {"services": [RTC_ENDPOINT]}})
    def test_matrixrtc_endpoints(self) -> None:
        channel = self.make_request("GET", f"{PATH_PREFIX}/rtc/services")
        self.assertEqual(401, channel.code)

        self.register_user("user", "password")
        tok = self.login("user", "password")
        channel = self.make_request("GET", f"{PATH_PREFIX}/rtc/services", access_token=tok)
        self.assertEqual(200, channel.code)

        self.assert_dict({"rtc_services": [RTC_ENDPOINT]}, channel.json_body)

