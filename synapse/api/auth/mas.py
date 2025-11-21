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
#
#
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)

from synapse.api.auth.base import BaseAuth
from synapse.api.errors import (
    AuthError,
    HttpResponseException,
    InvalidClientTokenError,
    SynapseError,
    UnrecognizedRequestError,
)
from synapse.http.site import SynapseRequest
from synapse.logging.opentracing import (
    active_span,
    force_tracing,
    inject_request_headers,
    start_active_span,
)
from synapse.metrics import SERVER_NAME_LABEL
from synapse.synapse_rust.http_client import HttpClient
from synapse.types import JsonDict, Requester, UserID, create_requester
from synapse.util.caches.cached_call import RetryOnExceptionCachedCall
from synapse.util.caches.response_cache import ResponseCache, ResponseCacheContext
from synapse.util.json import json_decoder

from . import introspection_response_timer

if TYPE_CHECKING:
    from synapse.rest.admin.experimental_features import ExperimentalFeature
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

# Scope as defined by MSC2967
# https://github.com/matrix-org/matrix-spec-proposals/pull/2967
UNSTABLE_SCOPE_MATRIX_API = "urn:matrix:org.matrix.msc2967.client:api:*"
UNSTABLE_SCOPE_MATRIX_DEVICE_PREFIX = "urn:matrix:org.matrix.msc2967.client:device:"
STABLE_SCOPE_MATRIX_API = "urn:matrix:client:api:*"
STABLE_SCOPE_MATRIX_DEVICE_PREFIX = "urn:matrix:client:device:"


class ServerMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    issuer: StrictStr
    account_management_uri: StrictStr


class IntrospectionResponse(BaseModel):
    retrieved_at_ms: StrictInt
    active: StrictBool
    scope: StrictStr | None = None
    username: StrictStr | None = None
    sub: StrictStr | None = None
    device_id: StrictStr | None = None
    expires_in: StrictInt | None = None
    model_config = ConfigDict(extra="allow")

    def get_scope_set(self) -> set[str]:
        if not self.scope:
            return set()

        return {token for token in self.scope.split(" ") if token}

    def is_active(self, now_ms: int) -> bool:
        if not self.active:
            return False

        # Compatibility tokens don't expire and don't have an 'expires_in' field
        if self.expires_in is None:
            return True

        absolute_expiry_ms = self.expires_in * 1000 + self.retrieved_at_ms
        return now_ms < absolute_expiry_ms


class MasDelegatedAuth(BaseAuth):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.server_name = hs.hostname
        self._clock = hs.get_clock()
        self._config = hs.config.mas

        self._http_client = hs.get_proxied_http_client()
        self._rust_http_client = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )
        self._server_metadata = RetryOnExceptionCachedCall[ServerMetadata](
            self._load_metadata
        )
        self._force_tracing_for_users = hs.config.tracing.force_tracing_for_users

        # # Token Introspection Cache
        # This remembers what users/devices are represented by which access tokens,
        # in order to reduce overall system load:
        # - on Synapse (as requests are relatively expensive)
        # - on the network
        # - on MAS
        #
        # Since there is no invalidation mechanism currently,
        # the entries expire after 2 minutes.
        # This does mean tokens can be treated as valid by Synapse
        # for longer than reality.
        #
        # Ideally, tokens should logically be invalidated in the following circumstances:
        # - If a session logout happens.
        #   In this case, MAS will delete the device within Synapse
        #   anyway and this is good enough as an invalidation.
        # - If the client refreshes their token in MAS.
        #   In this case, the device still exists and it's not the end of the world for
        #   the old access token to continue working for a short time.
        self._introspection_cache: ResponseCache[str] = ResponseCache(
            clock=self._clock,
            name="mas_token_introspection",
            server_name=self.server_name,
            timeout_ms=120_000,
            # don't log because the keys are access tokens
            enable_logging=False,
        )

    @property
    def _metadata_url(self) -> str:
        return (
            f"{str(self._config.endpoint).rstrip('/')}/.well-known/openid-configuration"
        )

    @property
    def _introspection_endpoint(self) -> str:
        return f"{str(self._config.endpoint).rstrip('/')}/oauth2/introspect"

    async def _load_metadata(self) -> ServerMetadata:
        response = await self._http_client.get_json(self._metadata_url)
        metadata = ServerMetadata(**response)
        return metadata

    async def issuer(self) -> str:
        metadata = await self._server_metadata.get()
        return metadata.issuer

    async def account_management_url(self) -> str:
        metadata = await self._server_metadata.get()
        return metadata.account_management_uri

    async def auth_metadata(self) -> JsonDict:
        metadata = await self._server_metadata.get()
        return metadata.dict()

    def is_request_using_the_shared_secret(self, request: SynapseRequest) -> bool:
        """
        Check if the request is using the shared secret.

        Args:
            request: The request to check.

        Returns:
            True if the request is using the shared secret, False otherwise.
        """
        access_token = self.get_access_token_from_request(request)
        shared_secret = self._config.secret()
        if not shared_secret:
            return False

        return access_token == shared_secret

    async def _introspect_token(
        self, token: str, cache_context: ResponseCacheContext[str]
    ) -> IntrospectionResponse:
        """
        Send a token to the introspection endpoint and returns the introspection response

        Parameters:
            token: The token to introspect

        Raises:
            HttpResponseException: If the introspection endpoint returns a non-2xx response
            ValueError: If the introspection endpoint returns an invalid JSON response
            JSONDecodeError: If the introspection endpoint returns a non-JSON response
            Exception: If the HTTP request fails

        Returns:
            The introspection response
        """

        # By default, we shouldn't cache the result unless we know it's valid
        cache_context.should_cache = False
        raw_headers: dict[str, str] = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Authorization": f"Bearer {self._config.secret()}",
            # Tell MAS that we support reading the device ID as an explicit
            # value, not encoded in the scope. This is supported by MAS 0.15+
            "X-MAS-Supports-Device-Id": "1",
        }

        args = {"token": token, "token_type_hint": "access_token"}
        body = urlencode(args, True)

        # Do the actual request

        logger.debug("Fetching token from MAS")
        start_time = self._clock.time()
        try:
            with start_active_span("mas-introspect-token"):
                inject_request_headers(raw_headers)
                resp_body = await self._rust_http_client.post(
                    url=self._introspection_endpoint,
                    response_limit=1 * 1024 * 1024,
                    headers=raw_headers,
                    request_body=body,
                )
        except HttpResponseException as e:
            end_time = self._clock.time()
            introspection_response_timer.labels(
                code=e.code, **{SERVER_NAME_LABEL: self.server_name}
            ).observe(end_time - start_time)
            raise
        except Exception:
            end_time = self._clock.time()
            introspection_response_timer.labels(
                code="ERR", **{SERVER_NAME_LABEL: self.server_name}
            ).observe(end_time - start_time)
            raise

        logger.debug("Fetched token from MAS")

        end_time = self._clock.time()
        introspection_response_timer.labels(
            code=200, **{SERVER_NAME_LABEL: self.server_name}
        ).observe(end_time - start_time)

        raw_response = json_decoder.decode(resp_body.decode("utf-8"))
        try:
            response = IntrospectionResponse(
                retrieved_at_ms=self._clock.time_msec(),
                **raw_response,
            )
        except ValidationError as e:
            raise ValueError(
                "The introspection endpoint returned an invalid JSON response"
            ) from e

        # We had a valid response, so we can cache it
        cache_context.should_cache = True
        return response

    async def is_server_admin(self, requester: Requester) -> bool:
        return "urn:synapse:admin:*" in requester.scope

    async def get_user_by_req(
        self,
        request: SynapseRequest,
        allow_guest: bool = False,
        allow_expired: bool = False,
        allow_locked: bool = False,
    ) -> Requester:
        parent_span = active_span()
        with start_active_span("get_user_by_req"):
            access_token = self.get_access_token_from_request(request)

            requester = await self.get_appservice_user(request, access_token)
            if not requester:
                requester = await self.get_user_by_access_token(
                    token=access_token,
                    allow_expired=allow_expired,
                )

            await self._record_request(request, requester)

            request.requester = requester

            if parent_span:
                if requester.authenticated_entity in self._force_tracing_for_users:
                    # request tracing is enabled for this user, so we need to force it
                    # tracing on for the parent span (which will be the servlet span).
                    #
                    # It's too late for the get_user_by_req span to inherit the setting,
                    # so we also force it on for that.
                    force_tracing()
                    force_tracing(parent_span)
                parent_span.set_tag(
                    "authenticated_entity", requester.authenticated_entity
                )
                parent_span.set_tag("user_id", requester.user.to_string())
                if requester.device_id is not None:
                    parent_span.set_tag("device_id", requester.device_id)
                if requester.app_service is not None:
                    parent_span.set_tag("appservice_id", requester.app_service.id)
            return requester

    async def get_user_by_access_token(
        self,
        token: str,
        allow_expired: bool = False,
    ) -> Requester:
        try:
            introspection_result = await self._introspection_cache.wrap(
                token, self._introspect_token, token, cache_context=True
            )
        except Exception:
            logger.exception("Failed to introspect token")
            raise SynapseError(503, "Unable to introspect the access token")

        logger.debug("Introspection result: %r", introspection_result)
        if not introspection_result.is_active(self._clock.time_msec()):
            raise InvalidClientTokenError("Token is not active")

        # Let's look at the scope
        scope = introspection_result.get_scope_set()

        # Determine type of user based on presence of particular scopes
        if (
            UNSTABLE_SCOPE_MATRIX_API not in scope
            and STABLE_SCOPE_MATRIX_API not in scope
        ):
            raise InvalidClientTokenError(
                "Token doesn't grant access to the Matrix C-S API"
            )

        if introspection_result.username is None:
            raise AuthError(
                500,
                "Invalid username claim in the introspection result",
            )

        user_id = UserID(
            localpart=introspection_result.username,
            domain=self.server_name,
        )

        # Try to find a user from the username claim
        user_info = await self.store.get_user_by_id(user_id=user_id.to_string())
        if user_info is None:
            raise AuthError(
                500,
                "User not found",
            )

        # MAS will give us the device ID as an explicit value for *compatibility* sessions
        # If present, we get it from here, if not we get it in the scope for next-gen sessions
        device_id = introspection_result.device_id
        if device_id is None:
            # Find device_ids in scope
            # We only allow a single device_id in the scope, so we find them all in the
            # scope list, and raise if there are more than one. The OIDC server should be
            # the one enforcing valid scopes, so we raise a 500 if we find an invalid scope.
            device_ids: set[str] = set()
            for tok in scope:
                if tok.startswith(UNSTABLE_SCOPE_MATRIX_DEVICE_PREFIX):
                    device_ids.add(tok[len(UNSTABLE_SCOPE_MATRIX_DEVICE_PREFIX) :])
                elif tok.startswith(STABLE_SCOPE_MATRIX_DEVICE_PREFIX):
                    device_ids.add(tok[len(STABLE_SCOPE_MATRIX_DEVICE_PREFIX) :])

            if len(device_ids) > 1:
                raise AuthError(
                    500,
                    "Multiple device IDs in scope",
                )

            device_id = next(iter(device_ids), None)

        if device_id is not None:
            # Sanity check the device_id
            if len(device_id) > 255 or len(device_id) < 1:
                raise AuthError(
                    500,
                    "Invalid device ID in introspection result",
                )

            # Make sure the device exists. This helps with introspection cache
            # invalidation: if we log out, the device gets deleted by MAS
            device = await self.store.get_device(
                user_id=user_id.to_string(),
                device_id=device_id,
            )
            if device is None:
                # Invalidate the introspection cache, the device was deleted
                self._introspection_cache.unset(token)
                raise InvalidClientTokenError("Token is not active")

        return create_requester(
            user_id=user_id,
            device_id=device_id,
            scope=scope,
        )

    async def get_user_by_req_experimental_feature(
        self,
        request: SynapseRequest,
        feature: "ExperimentalFeature",
        allow_guest: bool = False,
        allow_expired: bool = False,
        allow_locked: bool = False,
    ) -> Requester:
        try:
            requester = await self.get_user_by_req(
                request,
                allow_guest=allow_guest,
                allow_expired=allow_expired,
                allow_locked=allow_locked,
            )
            if await self.store.is_feature_enabled(requester.user.to_string(), feature):
                return requester

            raise UnrecognizedRequestError(code=404)
        except (AuthError, InvalidClientTokenError):
            if feature.is_globally_enabled(self.hs.config):
                # If its globally enabled then return the auth error
                raise

            raise UnrecognizedRequestError(code=404)
