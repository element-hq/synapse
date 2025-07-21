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


from typing import TYPE_CHECKING, cast

from synapse.api.errors import SynapseError
from synapse.http.server import DirectServeJsonResource

if TYPE_CHECKING:
    from synapse.app.generic_worker import GenericWorkerStore
    from synapse.http.site import SynapseRequest
    from synapse.server import HomeServer


class MasBaseResource(DirectServeJsonResource):
    def __init__(self, hs: "HomeServer"):
        # Importing this module requires authlib, which is an optional
        # dependency but required if msc3861 is enabled
        from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

        DirectServeJsonResource.__init__(self, extract_context=True)
        auth = hs.get_auth()
        assert isinstance(auth, MSC3861DelegatedAuth)
        self.msc3861_auth = auth
        self.store = cast("GenericWorkerStore", hs.get_datastores().main)
        self.hostname = hs.hostname

    def assert_request_is_from_mas(self, request: "SynapseRequest") -> None:
        """Assert that the request is coming from MAS itself, not a regular user.

        Throws a 403 if the request is not coming from MAS.
        """
        if not self.msc3861_auth.is_request_using_the_admin_token(request):
            raise SynapseError(403, "This endpoint must only be called by MAS")
