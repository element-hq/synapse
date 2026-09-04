#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from twisted.web.client import readBody
from twisted.web.http_headers import Headers

from synapse.api.errors import HttpResponseException, SynapseError
from synapse.logging.context import make_deferred_yieldable

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class _PolicyservCheckType:
    TEXT = "text"
    EVENT_ID = "event_id"


class ServerPolicyHandler:
    """Similar to the RoomPolicyHandler, but applies to the whole homeserver.

    Primarily used to interact with external-to-Synapse safety tooling.

    Current features include:
    * Search Redirection - When a user tries to search for a room in our directory, the
      query is first checked by a safety tool. If deemed unsafe or harmful, the search
      returns zero results and *may* include deterrence messaging as a redirect.

    The above is accomplished using policyserv-compatible "server-centric" API calls.
    For details, refer to the policyserv documentation: https://github.com/matrix-org/policyserv

    Note: policyserv is a specific Matrix policy server implementation. There is no standard
    Matrix specification for the policyserv API. This may change as the API surface is
    evaluated over time.
    """

    def __init__(self, hs: "HomeServer"):
        self._hs = hs
        self._federation_client = hs.get_federation_client()
        self._policyserv_url = hs.config.safety.policyserv_url
        self._policyserv_api_key = hs.config.safety.policyserv_api_key
        self._has_policyserv = bool(self._policyserv_url) and bool(
            self._policyserv_api_key
        )
        self._http_client = hs.get_proxied_http_client()
        self._enable_search_redirection = hs.config.safety.enable_search_redirection

    async def assert_neutral_search_query(self, query: str) -> None:
        """Asserts that the given search is neutral ("not unsafe or harmful").

        What specific criteria are used to determine neutrality is left as an implementation
        detail for the underlying policy provider. Typically, this will determine searches
        for illegal material to be unsafe or harmful.

        Args:
            query: The search query to be checked for safety.

        Raises:
            SynapseError: When the query is deemed unsafe or harmful. This may include
            deterrence messaging to discourage future, similar, searches.
        """
        if not self._has_policyserv or not self._enable_search_redirection:
            return  # disabled implicitly or explicitly - don't raise an error

        if not query:
            return  # nothing is being searched for - don't raise an error

        await self._policyserv_check(_PolicyservCheckType.TEXT, query.encode("utf-8"))

    async def _policyserv_check(self, check_type: str, body: bytes) -> None:
        """Performs a check against the policyserv Server-Centric Check API.

        Args:
            check_type: The type of check to perform. This is the last component of the
                check API to use. Try to use _PolicyservCheckType where possible.
            body: The request body to send to the given check API. Must already be
                formatted for that specific check type.

        Raises:
            SynapseError: When policyserv fails the check, or there was an error contacting
            the policyserv API.
        """

        # Do some quick asserts - we shouldn't be called if we don't have these details.
        assert self._policyserv_url is not None
        assert self._policyserv_api_key is not None

        # Call policyserv's check API, re-raising errors as Synapse errors if needed.
        try:
            response = await self._http_client.request(
                method="POST",
                uri=f"{self._policyserv_url}/_policyserv/v1/check/{check_type}",
                data=body,
                headers=Headers(
                    {
                        b"Authorization": [
                            b"Bearer " + self._policyserv_api_key.encode("utf-8")
                        ],
                    }
                ),
                timeout=3,  # somewhat arbitrary, but should be long enough for text matching
            )
        except HttpResponseException as ex:
            logger.info("HTTP error during policyserv request: %s", ex)
            raise ex.to_synapse_error()
        except Exception as ex:
            logger.exception("Error contacting policyserv: %s", ex)
            raise SynapseError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "unknown error contacting policyserv"
            )

        if response.code != 200:
            # error handling copied from BaseHttpClient.post_json_get_json
            body = await make_deferred_yieldable(readBody(response))
            response_ex = HttpResponseException(
                response.code, response.phrase.decode("ascii", errors="replace"), body
            ).to_synapse_error()
            logger.info("policyserv rejected request: %s", response_ex)
            raise response_ex
