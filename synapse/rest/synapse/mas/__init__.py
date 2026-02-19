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
# <https://www.gnu.org/licenses/agpl_3.0.html>.
#
#


import logging
from typing import TYPE_CHECKING

from twisted.web.resource import Resource

from synapse.rest.synapse.mas.devices import (
    MasDeleteDeviceResource,
    MasSyncDevicesResource,
    MasUpdateDeviceDisplayNameResource,
    MasUpsertDeviceResource,
)
from synapse.rest.synapse.mas.users import (
    MasAllowCrossSigningResetResource,
    MasDeleteUserResource,
    MasIsLocalpartAvailableResource,
    MasProvisionUserResource,
    MasQueryUserResource,
    MasReactivateUserResource,
    MasSetDisplayNameResource,
    MasUnsetDisplayNameResource,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)


class MasResource(Resource):
    """
    Provides endpoints for MAS to manage user accounts and devices.

    All endpoints are mounted under the path `/_synapse/mas/` and only work
    using the MAS admin token.
    """

    def __init__(self, hs: "HomeServer"):
        Resource.__init__(self)
        self.putChild(b"query_user", MasQueryUserResource(hs))
        self.putChild(b"provision_user", MasProvisionUserResource(hs))
        self.putChild(b"is_localpart_available", MasIsLocalpartAvailableResource(hs))
        self.putChild(b"delete_user", MasDeleteUserResource(hs))
        self.putChild(b"upsert_device", MasUpsertDeviceResource(hs))
        self.putChild(b"delete_device", MasDeleteDeviceResource(hs))
        self.putChild(
            b"update_device_display_name", MasUpdateDeviceDisplayNameResource(hs)
        )
        self.putChild(b"sync_devices", MasSyncDevicesResource(hs))
        self.putChild(b"reactivate_user", MasReactivateUserResource(hs))
        self.putChild(b"set_displayname", MasSetDisplayNameResource(hs))
        self.putChild(b"unset_displayname", MasUnsetDisplayNameResource(hs))
        self.putChild(
            b"allow_cross_signing_reset", MasAllowCrossSigningResetResource(hs)
        )
