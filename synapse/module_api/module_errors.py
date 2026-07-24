#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Exception types specific to the module API.

The definitions cannot live in `synapse.module_api.errors` because
`synapse.module_api` historically re-exports `synapse.api.errors` under the name
`errors`. Importing the `synapse.module_api.errors` submodule while initializing the
parent package would replace that re-export and could break existing modules.

The public `synapse.module_api.errors` module re-exports these exceptions, while the
parent package imports them directly from here to avoid that namespace collision.
"""

import attr


@attr.s(auto_attribs=True, slots=True)
class FederationHttpResponseException(Exception):
    """A remote homeserver returned an unsuccessful HTTP response."""

    remote_server_name: str
    status_code: int
    msg: str
    response_body: bytes


@attr.s(auto_attribs=True, slots=True)
class FederationHttpNotRetryingDestinationException(Exception):
    """Synapse is backing off federation requests to the remote homeserver."""

    remote_server_name: str


@attr.s(auto_attribs=True, slots=True)
class FederationHttpDeniedException(Exception):
    """The remote homeserver is excluded by Synapse's federation policy."""

    remote_server_name: str


@attr.s(auto_attribs=True, slots=True)
class FederationHttpRequestSendFailedException(Exception):
    """Synapse could not send or decode a federation HTTP request."""

    remote_server_name: str
    can_retry: bool
