#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from twisted.web.resource import Resource

from synapse.rest.synapse.client import build_synapse_client_resource_tree
from synapse.types import JsonDict

from tests import unittest


class BaseTestCase(unittest.HomeserverTestCase):
    SHARED_SECRET = "shared_secret"

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config["enable_registration"] = False
        config["experimental_features"] = {
            "msc3861": {
                "enabled": True,
                "issuer": "https://example.com",
                "client_id": "dummy",
                "client_auth_method": "client_secret_basic",
                "client_secret": "dummy",
                "admin_token": self.SHARED_SECRET,
            }
        }
        return config

    def create_resource_dict(self) -> dict[str, Resource]:
        base = super().create_resource_dict()
        base.update(build_synapse_client_resource_tree(self.hs))
        return base
