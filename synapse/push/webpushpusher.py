#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2024 Mathieu Velten
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
import json
import logging
import os
import time
from base64 import urlsafe_b64encode
from hashlib import blake2s
from typing import TYPE_CHECKING, List, Optional, Union, cast
from urllib.parse import urlparse

from py_vapid import Vapid
from pywebpush import CaseInsensitiveDict, WebPusher

from twisted.internet import defer
from twisted.web.client import readBody
from twisted.web.http_headers import Headers
from twisted.web.iweb import IResponse

from synapse.http.client import SimpleHttpClient
from synapse.push import PusherConfig, PusherConfigException
from synapse.push.httppusher import HttpPusher
from synapse.types import JsonDict, JsonMapping

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# Max payload size is 4096
MAX_BODY_LENGTH = 1000
MAX_CIPHERTEXT_LENGTH = 2000


class WebPushPusher(HttpPusher):
    def __init__(self, hs: "HomeServer", pusher_config: PusherConfig):
        super().__init__(hs, pusher_config)

        self.endpoint: str = self.data.get("endpoint")  # type: ignore[assignment]
        if not isinstance(self.endpoint, str):
            raise PusherConfigException(
                "'endpoint' required in data for WebPush pusher and must be a string"
            )

        self.auth: str = self.data.get("auth")  # type: ignore[assignment]
        if not isinstance(self.auth, str):
            raise PusherConfigException(
                "'auth' required in data for WebPush pusher and must be a string"
            )

        self.http_client = hs.get_proxied_blocklisted_http_client()

        self.msc4174_config = hs.config.experimental.msc4174

        self.cached_vapid_headers: Optional[JsonDict] = None
        self.cached_vapid_headers_expires: int = 0

    async def dispatch_push(
        self,
        content: JsonDict,
        tweaks: Optional[JsonMapping] = None,
        default_payload: Optional[JsonMapping] = None,
    ) -> Union[bool, List[str]]:
        content = content.copy()

        if default_payload:
            content.update(default_payload)

        counts = content.pop("counts", None)
        if counts is not None:
            for attr in ["unread", "missed_calls"]:
                count_value = counts.get(attr, None)
                if count_value is not None:
                    content[attr] = count_value

        # we can't show formatted_body in a notification anyway on web
        # so remove it
        content.pop("formatted_body", None)
        body = content.get("body")
        # make some attempts to not go over the max payload length
        if isinstance(body, str) and len(body) > MAX_BODY_LENGTH:
            content["body"] = body[0 : MAX_BODY_LENGTH - 1] + "…"
        ciphertext = content.get("ciphertext")
        if isinstance(ciphertext, str) and len(ciphertext) > MAX_CIPHERTEXT_LENGTH:
            content.pop("ciphertext", None)

        # drop notifications without an event id if requested,
        # see https://github.com/matrix-org/sygnal/issues/186
        if content.get("events_only") is True and not content.get("event_id"):
            return []

        return await self.send_webpush(content)

    async def send_webpush(self, content: JsonDict) -> Union[bool, List[str]]:
        # web push only supports normal and low priority, so assume normal if absent
        low_priority = content.get("prio") == "low"
        # allow dropping earlier notifications in the same room if requested
        topic = None
        room_id = content.get("room_id")
        if room_id and content.get("only_last_per_room") is True:
            # ask for a 22 byte hash, so the base64 of it is 32,
            # the limit webpush allows for the topic
            topic = urlsafe_b64encode(
                blake2s(room_id.encode(), digest_size=22).digest()
            )

        subscription_info = {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.pushkey, "auth": self.auth},
        }

        # We cache the signed VAPID header as recommended by the spec, to avoid calculating a
        # new one each time, and allowavoid the push server to cache it too and avoid verification.
        if (
            not self.cached_vapid_headers
            or time.time() > self.cached_vapid_headers_expires - 60
        ):
            vapid_claims: JsonDict = {
                "sub": "mailto:{}".format(self.msc4174_config.vapid_contact_email),
            }

            url = urlparse(cast(str, subscription_info.get("endpoint")))
            aud = "{}://{}".format(url.scheme, url.netloc)
            vapid_claims["aud"] = aud

            # encryption lives for 12 hours
            vapid_claims["exp"] = int(time.time()) + (12 * 60 * 60)

            if os.path.isfile(self.msc4174_config.vapid_private_key):
                vv = Vapid.from_file(
                    private_key_file=self.msc4174_config.vapid_private_key
                )
            else:
                vv = Vapid.from_string(
                    private_key=self.msc4174_config.vapid_private_key
                )

            self.cached_vapid_headers_expires = vapid_claims["exp"]
            self.cached_vapid_headers = vv.sign(vapid_claims)

        request = WebPusher(
            subscription_info, requests_session=HTTP_REQUEST_FACTORY
        ).send(
            data=json.dumps(content),
            headers=self.cached_vapid_headers,
            ttl=self.msc4174_config.ttl,
        )

        response = await request.execute(self.http_client, low_priority, topic)
        response_text = (await readBody(response)).decode()

        reject_pushkey = self._handle_response(
            response, response_text, self.pushkey, self.endpoint
        )
        if reject_pushkey:
            return [self.pushkey]

        return True

    async def send_badge(self, badge: int) -> bool:
        """
        Args:
            badge: number of unread messages
        """
        logger.debug("Sending updated badge count %d to %s", badge, self.name)
        content = {
            "id": "",
            "type": None,
            "sender": "",
            "counts": {"unread": badge},
        }
        if await self.send_webpush(content):
            return True
        else:
            logger.warning("Failed to send badge count to %s", self.name)
            return False

    def _handle_response(
        self,
        response: IResponse,
        response_text: str,
        pushkey: str,
        endpoint_domain: str,
    ) -> bool:
        """
        Logs and determines the outcome of the response

        Returns:
            Boolean whether the puskey should be rejected
        """
        ttl_response_headers = response.headers.getRawHeaders(b"TTL")
        if ttl_response_headers:
            try:
                ttl_given = int(ttl_response_headers[0])
                if ttl_given != self.msc4174_config.ttl:
                    logger.info(
                        "requested TTL of %d to endpoint %s but got %d",
                        self.msc4174_config.ttl,
                        endpoint_domain,
                        ttl_given,
                    )
            except ValueError:
                pass
        # permanent errors
        if response.code == 404 or response.code == 410:
            logger.warning(
                "Rejecting pushkey %s; subscription is invalid on %s: %d: %s",
                pushkey,
                endpoint_domain,
                response.code,
                response_text,
            )
            return True
        # and temporary ones
        if response.code >= 400:
            logger.warning(
                "webpush request failed for pushkey %s; %s responded with %d: %s",
                pushkey,
                endpoint_domain,
                response.code,
                response_text,
            )
        elif response.code != 201:
            logger.info(
                "webpush request for pushkey %s didn't respond with 201; "
                + "%s responded with %d: %s",
                pushkey,
                endpoint_domain,
                response.code,
                response_text,
            )
        return False


class HttpRequestFactory:
    """
    Provide a post method that matches the API expected from pywebpush.
    """

    def post(
        self,
        endpoint: str,
        data: bytes,
        headers: CaseInsensitiveDict,
        timeout: int,
    ) -> "HttpDelayedRequest":
        """
        Convert the requests-like API to a Twisted API call.

        Args:
            endpoint:
                The full http url to post to
            data:
                the (encrypted) binary body of the request
            headers:
                A (costume) dictionary with the headers.
            timeout:
                Ignored for now
        """
        return HttpDelayedRequest(endpoint, data, headers)


HTTP_REQUEST_FACTORY = HttpRequestFactory()


class HttpDelayedRequest:
    """
    Captures the values received from pywebpush for the endpoint request.
    The request isn't immediately executed, to allow adding headers
    not supported by pywebpush, like Topic and Urgency.

    Also provides the interface that pywebpush expects from a response object.
    pywebpush expects a synchronous API, while we use an asynchronous API.

    To keep pywebpush happy we present it with some hardcoded values that
    make its assertions pass even though the HTTP request has not yet been
    made.

    Attributes:
        status_code:
            Defined to be 200 so the pywebpush check to see if is below 202
            passes.
        text:
            Set to None as pywebpush references this field for its logging.
    """

    status_code: int = 200
    text: Optional[str] = None

    def __init__(self, endpoint: str, data: bytes, vapid_headers: CaseInsensitiveDict):
        self.endpoint = endpoint
        self.data = data
        self.vapid_headers = vapid_headers

    def execute(
        self, http_client: SimpleHttpClient, low_priority: bool, topic: bytes
    ) -> defer.Deferred[IResponse]:
        # Convert the headers to the camelcase version.
        headers = {
            b"User-Agent": ["sygnal"],
            b"Content-Encoding": [self.vapid_headers["content-encoding"]],
            b"Authorization": [self.vapid_headers["authorization"]],
            b"TTL": [self.vapid_headers["ttl"]],
            b"Urgency": ["low" if low_priority else "normal"],
        }
        if topic:
            headers[b"Topic"] = [topic]
        return defer.ensureDeferred(
            http_client.request(
                "POST",
                self.endpoint,
                headers=Headers(headers),
                data=self.data,
            )
        )
