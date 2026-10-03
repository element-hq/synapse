#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2016 OpenMarket Ltd
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

import hashlib
import logging
from typing import TYPE_CHECKING

from canonicaljson import encode_canonical_json
from unpaddedbase64 import encode_base64

from synapse.api.errors import SynapseError
from synapse.http import servlet
from synapse.http.server import HttpServer
from synapse.http.servlet import assert_params_in_dict, parse_json_object_from_request
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import set_tag
from synapse.rest.client.transactions import HttpTransactionCache
from synapse.types import JsonDict, Requester
from synapse.util.caches.lrucache import LruCache

from ._base import client_patterns

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

UNKNOWN_RECIPIENT_SET = "ORG.MATRIX.EXPERIMENTAL.UNKNOWN_RECIPIENT_SET"

RecipientSet = frozenset[tuple[str, str]]


def recipient_set_hash(recipients: RecipientSet) -> str:
    """Unpadded base64 of the SHA-256 of the canonical JSON of the sorted
    `[user_id, device_id]` pairs."""
    pairs = [list(pair) for pair in sorted(recipients)]
    return encode_base64(hashlib.sha256(encode_canonical_json(pairs)).digest())


def _parse_pairs(value: object, name: str) -> RecipientSet:
    if not isinstance(value, list) or not all(
        isinstance(pair, list)
        and len(pair) == 2
        and all(isinstance(part, str) for part in pair)
        for pair in value
    ):
        raise SynapseError(400, f"'{name}' must be a list of [user_id, device_id]")
    return frozenset((user_id, device_id) for user_id, device_id in value)


class SendToDeviceRestServlet(servlet.RestServlet):
    PATTERNS = client_patterns(
        "/sendToDevice/(?P<message_type>[^/]*)/(?P<txn_id>[^/]*)$"
    )
    CATEGORY = "The to_device stream"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.txns = HttpTransactionCache(hs)
        self.device_message_handler = hs.get_device_message_handler()
        self._shared_enabled = hs.config.experimental.photon_shared_to_device_enabled
        # (user_id, device_id, set hash) -> set. Soft state: a miss makes the
        # client resend the full list, so it can be evicted, lost on restart
        # and differ between workers.
        # ponytail: whole sets per entry; store deltas if memory bites.
        self._recipient_sets: LruCache[tuple[str, str, str], RecipientSet] = LruCache(
            max_size=1000,
            clock=hs.get_clock(),
            server_name=hs.hostname,
            cache_name="photon_recipient_sets",
        )

    async def on_PUT(
        self, request: SynapseRequest, message_type: str, txn_id: str
    ) -> tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        set_tag("txn_id", txn_id)
        return await self.txns.fetch_or_execute_request(
            request,
            requester,
            self._put,
            request,
            requester,
            message_type,
        )

    async def _put(
        self,
        request: SynapseRequest,
        requester: Requester,
        message_type: str,
    ) -> tuple[int, JsonDict]:
        content = parse_json_object_from_request(request)
        if self._shared_enabled and "recipients" in content:
            messages = self._expand_shared(requester, content)
        else:
            assert_params_in_dict(content, ("messages",))
            messages = content["messages"]

        await self.device_message_handler.send_device_message(
            requester, message_type, messages
        )

        return 200, {}

    def _expand_shared(self, requester: Requester, body: JsonDict) -> JsonDict:
        """Turn `{"content", "recipients": {"base", "add", "remove"}}` into
        ordinary per-device `messages`, caching the resulting recipient set."""
        recipients = body["recipients"]
        if "messages" in body or not isinstance(recipients, dict):
            raise SynapseError(
                400, "'recipients' must be an object, without 'messages'"
            )
        assert_params_in_dict(body, ("content",))
        if not isinstance(body["content"], dict):
            raise SynapseError(400, "'content' must be an object")

        owner = (requester.user.to_string(), requester.device_id or "")
        targets: RecipientSet = frozenset()
        base = recipients.get("base")
        if base is not None:
            cached = self._recipient_sets.get((*owner, base))
            if cached is None:
                raise SynapseError(
                    400,
                    "Unknown recipient set; send the full list",
                    UNKNOWN_RECIPIENT_SET,
                )
            targets = cached
        targets = (targets - _parse_pairs(recipients.get("remove", []), "remove")) | (
            _parse_pairs(recipients.get("add", []), "add")
        )
        self._recipient_sets.set((*owner, recipient_set_hash(targets)), targets)

        messages: dict[str, dict[str, JsonDict]] = {}
        for user_id, device_id in targets:
            messages.setdefault(user_id, {})[device_id] = body["content"]
        return messages


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    SendToDeviceRestServlet(hs).register(http_server)
