#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple, cast

from synapse.api.errors import (
    InteractiveAuthIncompleteError,
    InvalidAPICallError,
    SynapseError,
)
from synapse.http.server import HttpServer
from synapse.http.servlet import (
    RestServlet,
    parse_integer,
    parse_json_object_from_request,
    parse_string,
)
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import log_kv, set_tag
from synapse.rest.client._base import client_patterns, interactive_auth_handler
from synapse.types import JsonDict, StreamToken
from synapse.util.cancellation import cancellable

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class KeyUploadServlet(RestServlet):
    """
    POST /keys/upload HTTP/1.1
    Content-Type: application/json

    {
        "device_keys": {
            "user_id": "<user_id>",
            "device_id": "<device_id>",
            "valid_until_ts": <millisecond_timestamp>,
            "algorithms": [
                "m.olm.curve25519-aes-sha2",
            ]
            "keys": {
                "<algorithm>:<device_id>": "<key_base64>",
            },
            "signatures:" {
                "<user_id>" {
                    "<algorithm>:<device_id>": "<signature_base64>"
                }
            }
        },
        "fallback_keys": {
            "<algorithm>:<device_id>": "<key_base64>",
            "signed_<algorithm>:<device_id>": {
                "fallback": true,
                "key": "<key_base64>",
                "signatures": {
                    "<user_id>": {
                        "<algorithm>:<device_id>": "<key_base64>"
                    }
                }
            }
        }
        "one_time_keys": {
            "<algorithm>:<key_id>": "<key_base64>"
        },
    }

    response, e.g.:

    {
        "one_time_key_counts": {
            "curve25519": 10,
            "signed_curve25519": 20
        }
    }

    """

    PATTERNS = client_patterns("/keys/upload(/(?P<device_id>[^/]+))?$")
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()
        self.device_handler = hs.get_device_handler()
        self._clock = hs.get_clock()
        self._store = hs.get_datastores().main

    async def on_POST(
        self, request: SynapseRequest, device_id: Optional[str]
    ) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user_id = requester.user.to_string()
        body = parse_json_object_from_request(request)

        if device_id is not None:
            # Providing the device_id should only be done for setting keys
            # for dehydrated devices; however, we allow it for any device for
            # compatibility with older clients.
            if requester.device_id is not None and device_id != requester.device_id:
                dehydrated_device = await self.device_handler.get_dehydrated_device(
                    user_id
                )
                if dehydrated_device is not None and device_id != dehydrated_device[0]:
                    set_tag("error", True)
                    log_kv(
                        {
                            "message": "Client uploading keys for a different device",
                            "logged_in_id": requester.device_id,
                            "key_being_uploaded": device_id,
                        }
                    )
                    logger.warning(
                        "Client uploading keys for a different device "
                        "(logged in as %s, uploading for %s)",
                        requester.device_id,
                        device_id,
                    )
        else:
            device_id = requester.device_id

        if device_id is None:
            raise SynapseError(
                400, "To upload keys, you must pass device_id when authenticating"
            )

        result = await self.e2e_keys_handler.upload_keys_for_user(
            user_id=user_id, device_id=device_id, keys=body
        )

        return 200, result


class KeyQueryServlet(RestServlet):
    """
    POST /keys/query HTTP/1.1
    Content-Type: application/json
    {
      "device_keys": {
        "<user_id>": ["<device_id>"]
    } }

    HTTP/1.1 200 OK
    {
      "device_keys": {
        "<user_id>": {
          "<device_id>": {
            "user_id": "<user_id>", // Duplicated to be signed
            "device_id": "<device_id>", // Duplicated to be signed
            "valid_until_ts": <millisecond_timestamp>,
            "algorithms": [ // List of supported algorithms
              "m.olm.curve25519-aes-sha2",
            ],
            "keys": { // Must include a ed25519 signing key
              "<algorithm>:<key_id>": "<key_base64>",
            },
            "signatures:" {
              // Must be signed with device's ed25519 key
              "<user_id>/<device_id>": {
                "<algorithm>:<key_id>": "<signature_base64>"
              }
              // Must be signed by this server.
              "<server_name>": {
                "<algorithm>:<key_id>": "<signature_base64>"
    } } } } } }
    """

    PATTERNS = client_patterns("/keys/query$")
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    @cancellable
    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user_id = requester.user.to_string()
        device_id = requester.device_id
        timeout = parse_integer(request, "timeout", 10 * 1000)
        body = parse_json_object_from_request(request)

        device_keys = body.get("device_keys")
        if not isinstance(device_keys, dict):
            raise InvalidAPICallError("'device_keys' must be a JSON object")

        def is_list_of_strings(values: Any) -> bool:
            return isinstance(values, list) and all(isinstance(v, str) for v in values)

        if any(not is_list_of_strings(keys) for keys in device_keys.values()):
            raise InvalidAPICallError(
                "'device_keys' values must be a list of strings",
            )

        result = await self.e2e_keys_handler.query_devices(
            body, timeout, user_id, device_id
        )
        return 200, result


class KeyChangesServlet(RestServlet):
    """Returns the list of changes of keys between two stream tokens (may return
    spurious extra results, since we currently ignore the `to` param).

        GET /keys/changes?from=...&to=...

        200 OK
        { "changed": ["@foo:example.com"] }
    """

    PATTERNS = client_patterns("/keys/changes$")
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.device_handler = hs.get_device_handler()
        self.store = hs.get_datastores().main

    @cancellable
    async def on_GET(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)

        from_token_string = parse_string(request, "from", required=True)
        set_tag("from", from_token_string)

        # We want to enforce they do pass us one, but we ignore it and return
        # changes after the "to" as well as before.
        #
        # XXX This does not enforce that "to" is passed.
        set_tag("to", str(parse_string(request, "to")))

        from_token = await StreamToken.from_string(self.store, from_token_string)

        user_id = requester.user.to_string()

        device_list_updates = await self.device_handler.get_user_ids_changed(
            user_id, from_token
        )

        response: JsonDict = {}
        response["changed"] = list(device_list_updates.changed)
        response["left"] = list(device_list_updates.left)

        return 200, response


class OneTimeKeyServlet(RestServlet):
    """
    POST /keys/claim HTTP/1.1
    {
      "one_time_keys": {
        "<user_id>": {
          "<device_id>": "<algorithm>"
    } } }

    HTTP/1.1 200 OK
    {
      "one_time_keys": {
        "<user_id>": {
          "<device_id>": {
            "<algorithm>:<key_id>": "<key_base64>"
    } } } }

    """

    PATTERNS = client_patterns("/keys/claim$")
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        timeout = parse_integer(request, "timeout", 10 * 1000)
        body = parse_json_object_from_request(request)

        # Generate a count for each algorithm, which is hard-coded to 1.
        query: Dict[str, Dict[str, Dict[str, int]]] = {}
        for user_id, one_time_keys in body.get("one_time_keys", {}).items():
            for device_id, algorithm in one_time_keys.items():
                query.setdefault(user_id, {})[device_id] = {algorithm: 1}

        result = await self.e2e_keys_handler.claim_one_time_keys(
            query, requester.user, timeout, always_include_fallback_keys=False
        )
        return 200, result


class UnstableOneTimeKeyServlet(RestServlet):
    """
    Identical to the stable endpoint (OneTimeKeyServlet) except it allows for
    querying for multiple OTKs at once and always includes fallback keys in the
    response.

    POST /keys/claim HTTP/1.1
    {
      "one_time_keys": {
        "<user_id>": {
          "<device_id>": ["<algorithm>", ...]
    } } }

    HTTP/1.1 200 OK
    {
      "one_time_keys": {
        "<user_id>": {
          "<device_id>": {
            "<algorithm>:<key_id>": "<key_base64>"
    } } } }

    """

    PATTERNS = [re.compile(r"^/_matrix/client/unstable/org.matrix.msc3983/keys/claim$")]
    CATEGORY = "Encryption requests"

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        timeout = parse_integer(request, "timeout", 10 * 1000)
        body = parse_json_object_from_request(request)

        # Generate a count for each algorithm.
        query: Dict[str, Dict[str, Dict[str, int]]] = {}
        for user_id, one_time_keys in body.get("one_time_keys", {}).items():
            for device_id, algorithms in one_time_keys.items():
                query.setdefault(user_id, {})[device_id] = Counter(algorithms)

        result = await self.e2e_keys_handler.claim_one_time_keys(
            query, requester.user, timeout, always_include_fallback_keys=True
        )
        return 200, result


class SigningKeyUploadServlet(RestServlet):
    """
    POST /keys/device_signing/upload HTTP/1.1
    Content-Type: application/json

    {
    }
    """

    PATTERNS = client_patterns("/keys/device_signing/upload$", releases=("v3",))

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.hs = hs
        self.auth = hs.get_auth()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()
        self.auth_handler = hs.get_auth_handler()

    @interactive_auth_handler
    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request)
        user_id = requester.user.to_string()
        body = parse_json_object_from_request(request)

        (
            is_cross_signing_setup,
            master_key_updatable_without_uia,
        ) = await self.e2e_keys_handler.check_cross_signing_setup(user_id)

        # Resending exactly the same keys should just 200 OK without doing a UIA prompt.
        keys_are_different = await self.e2e_keys_handler.has_different_keys(
            user_id, body
        )
        if not keys_are_different:
            return 200, {}

        # The keys are different; is x-signing set up? If no, then this is first-time
        # setup, and that is allowed without UIA, per MSC3967.
        # If yes, then we need to authenticate the change.
        if is_cross_signing_setup:
            # With MSC3861, UIA is not possible. Instead, the auth service has to
            # explicitly mark the master key as replaceable.
            if self.hs.config.experimental.msc3861.enabled:
                if not master_key_updatable_without_uia:
                    # If MSC3861 is enabled, we can assume self.auth is an instance of MSC3861DelegatedAuth
                    # We import lazily here because of the authlib requirement
                    from synapse.api.auth.msc3861_delegated import MSC3861DelegatedAuth

                    auth = cast(MSC3861DelegatedAuth, self.auth)

                    uri = await auth.account_management_url()
                    if uri is not None:
                        url = f"{uri}?action=org.matrix.cross_signing_reset"
                    else:
                        url = await auth.issuer()

                    # We use a dummy session ID as this isn't really a UIA flow, but we
                    # reuse the same API shape for better client compatibility.
                    raise InteractiveAuthIncompleteError(
                        "dummy",
                        {
                            "session": "dummy",
                            "flows": [
                                {"stages": ["org.matrix.cross_signing_reset"]},
                            ],
                            "params": {
                                "org.matrix.cross_signing_reset": {
                                    "url": url,
                                },
                            },
                            "msg": "To reset your end-to-end encryption cross-signing "
                            f"identity, you first need to approve it at {url} and "
                            "then try again.",
                        },
                    )
            else:
                # Without MSC3861, we require UIA.
                await self.auth_handler.validate_user_via_ui_auth(
                    requester,
                    request,
                    body,
                    "reset the device signing key on your account",
                    # Do not allow skipping of UIA auth.
                    can_skip_ui_auth=False,
                )

        result = await self.e2e_keys_handler.upload_signing_keys_for_user(user_id, body)
        return 200, result


class SignaturesUploadServlet(RestServlet):
    """
    POST /keys/signatures/upload HTTP/1.1
    Content-Type: application/json

    {
      "@alice:example.com": {
        "<device_id>": {
          "user_id": "<user_id>",
          "device_id": "<device_id>",
          "algorithms": [
            "m.olm.curve25519-aes-sha2",
            "m.megolm.v1.aes-sha2"
          ],
          "keys": {
            "<algorithm>:<device_id>": "<key_base64>",
          },
          "signatures": {
            "<signing_user_id>": {
              "<algorithm>:<signing_key_base64>": "<signature_base64>>"
            }
          }
        }
      }
    }
    """

    PATTERNS = client_patterns("/keys/signatures/upload$")

    def __init__(self, hs: "HomeServer"):
        super().__init__()
        self.auth = hs.get_auth()
        self.e2e_keys_handler = hs.get_e2e_keys_handler()

    async def on_POST(self, request: SynapseRequest) -> Tuple[int, JsonDict]:
        requester = await self.auth.get_user_by_req(request, allow_guest=True)
        user_id = requester.user.to_string()
        body = parse_json_object_from_request(request)

        result = await self.e2e_keys_handler.upload_signatures_for_device_keys(
            user_id, body
        )
        return 200, result


def register_servlets(hs: "HomeServer", http_server: HttpServer) -> None:
    KeyUploadServlet(hs).register(http_server)
    KeyQueryServlet(hs).register(http_server)
    KeyChangesServlet(hs).register(http_server)
    OneTimeKeyServlet(hs).register(http_server)
    if hs.config.experimental.msc3983_appservice_otk_claims:
        UnstableOneTimeKeyServlet(hs).register(http_server)
    if hs.config.worker.worker_app is None:
        SigningKeyUploadServlet(hs).register(http_server)
        SignaturesUploadServlet(hs).register(http_server)
