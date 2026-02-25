#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation.
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlencode

from authlib.oauth2 import ClientAuth
from authlib.oauth2.auth import encode_client_secret_basic, encode_client_secret_post
from authlib.oauth2.rfc7523 import ClientSecretJWT, PrivateKeyJWT, private_key_jwt_sign
from authlib.oauth2.rfc7662 import IntrospectionToken
from authlib.oidc.discovery import OpenIDProviderMetadata, get_well_known_url

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
from synapse.types import Requester, UserID, create_requester
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

# Scope which allows access to the Synapse admin API
SCOPE_SYNAPSE_ADMIN = "urn:synapse:admin:*"


def scope_to_list(scope: str) -> list[str]:
    """Convert a scope string to a list of scope tokens"""
    return scope.strip().split(" ")


@dataclass
class IntrospectionResult:
    _inner: IntrospectionToken

    # when we retrieved this token,
    # in milliseconds since the Unix epoch
    retrieved_at_ms: int

    def is_active(self, now_ms: int) -> bool:
        if not self._inner.get("active"):
            return False

        expires_in = self._inner.get("expires_in")
        if expires_in is None:
            return True
        if not isinstance(expires_in, int):
            raise InvalidClientTokenError("token `expires_in` is not an int")

        absolute_expiry_ms = expires_in * 1000 + self.retrieved_at_ms
        return now_ms < absolute_expiry_ms

    def get_scope_list(self) -> list[str]:
        value = self._inner.get("scope")
        if not isinstance(value, str):
            return []
        return scope_to_list(value)

    def get_sub(self) -> str | None:
        value = self._inner.get("sub")
        if not isinstance(value, str):
            return None
        return value

    def get_username(self) -> str | None:
        value = self._inner.get("username")
        if not isinstance(value, str):
            return None
        return value

    def get_name(self) -> str | None:
        value = self._inner.get("name")
        if not isinstance(value, str):
            return None
        return value

    def get_device_id(self) -> str | None:
        value = self._inner.get("device_id")
        if value is not None and not isinstance(value, str):
            raise AuthError(
                500,
                "Invalid device ID in introspection result",
            )
        return value


class PrivateKeyJWTWithKid(PrivateKeyJWT):  # type: ignore[misc]
    """An implementation of the private_key_jwt client auth method that includes a kid header.

    This is needed because some providers (Keycloak) require the kid header to figure
    out which key to use to verify the signature.
    """

    def sign(self, auth: Any, token_endpoint: str) -> bytes:
        return private_key_jwt_sign(
            auth.client_secret,
            client_id=auth.client_id,
            token_endpoint=token_endpoint,
            claims=self.claims,
            header={"kid": auth.client_secret["kid"]},
        )


class MSC3861DelegatedAuth(BaseAuth):
    AUTH_METHODS = {
        "client_secret_post": encode_client_secret_post,
        "client_secret_basic": encode_client_secret_basic,
        "client_secret_jwt": ClientSecretJWT(),
        "private_key_jwt": PrivateKeyJWTWithKid(),
    }

    EXTERNAL_ID_PROVIDER = "oauth-delegated"

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self._config = hs.config.experimental.msc3861
        auth_method = MSC3861DelegatedAuth.AUTH_METHODS.get(
            self._config.client_auth_method.value, None
        )
        # Those assertions are already checked when parsing the config
        assert self._config.enabled, "OAuth delegation is not enabled"
        assert self._config.issuer, "No issuer provided"
        assert self._config.client_id, "No client_id provided"
        assert auth_method is not None, "Invalid client_auth_method provided"

        self.server_name = hs.hostname
        self._clock = hs.get_clock()
        self._http_client = hs.get_proxied_http_client()
        self._hostname = hs.hostname
        self._admin_token: Callable[[], str | None] = self._config.admin_token
        self._force_tracing_for_users = hs.config.tracing.force_tracing_for_users

        self._rust_http_client = HttpClient(
            reactor=hs.get_reactor(),
            user_agent=self._http_client.user_agent.decode("utf8"),
        )

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
            name="token_introspection",
            server_name=self.server_name,
            timeout_ms=120_000,
            # don't log because the keys are access tokens
            enable_logging=False,
        )

        self._issuer_metadata = RetryOnExceptionCachedCall[OpenIDProviderMetadata](
            self._load_metadata
        )

        if isinstance(auth_method, PrivateKeyJWTWithKid):
            # Use the JWK as the client secret when using the private_key_jwt method
            assert self._config.jwk, "No JWK provided"
            self._client_auth = ClientAuth(
                self._config.client_id, self._config.jwk, auth_method
            )
        else:
            # Else use the client secret
            client_secret = self._config.client_secret()
            assert client_secret, "No client_secret provided"
            self._client_auth = ClientAuth(
                self._config.client_id, client_secret, auth_method
            )

    async def _load_metadata(self) -> OpenIDProviderMetadata:
        if self._config.issuer_metadata is not None:
            return OpenIDProviderMetadata(**self._config.issuer_metadata)
        url = get_well_known_url(self._config.issuer, external=True)
        response = await self._http_client.get_json(url)
        metadata = OpenIDProviderMetadata(**response)
        # metadata.validate_introspection_endpoint()
        return metadata

    async def issuer(self) -> str:
        """
        Get the configured issuer

        This will use the issuer value set in the metadata,
        falling back to the one set in the config if not set in the metadata
        """
        metadata = await self._issuer_metadata.get()
        return metadata.issuer or self._config.issuer

    async def account_management_url(self) -> str | None:
        """
        Get the configured account management URL

        This will discover the account management URL from the issuer if it's not set in the config
        """
        if self._config.account_management_url is not None:
            return self._config.account_management_url

        try:
            metadata = await self._issuer_metadata.get()
            return metadata.get("account_management_uri", None)
        # We don't want to raise here if we can't load the metadata
        except Exception:
            logger.warning("Failed to load metadata:", exc_info=True)
            return None

    async def auth_metadata(self) -> dict[str, Any]:
        """
        Returns the auth metadata dict
        """
        return await self._issuer_metadata.get()

    async def _introspection_endpoint(self) -> str:
        """
        Returns the introspection endpoint of the issuer

        It uses the config option if set, otherwise it will use OIDC discovery to get it
        """
        if self._config.introspection_endpoint is not None:
            return self._config.introspection_endpoint

        metadata = await self._issuer_metadata.get()
        return metadata.get("introspection_endpoint")

    async def _introspect_token(
        self, token: str, cache_context: ResponseCacheContext[str]
    ) -> IntrospectionResult:
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
        introspection_endpoint = await self._introspection_endpoint()
        raw_headers: dict[str, str] = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            # Tell MAS that we support reading the device ID as an explicit
            # value, not encoded in the scope. This is supported by MAS 0.15+
            "X-MAS-Supports-Device-Id": "1",
        }

        args = {"token": token, "token_type_hint": "access_token"}
        body = urlencode(args, True)

        # Fill the body/headers with credentials
        uri, raw_headers, body = self._client_auth.prepare(
            method="POST", uri=introspection_endpoint, headers=raw_headers, body=body
        )

        # Do the actual request

        logger.debug("Fetching token from MAS")
        start_time = self._clock.time()
        try:
            with start_active_span("mas-introspect-token"):
                inject_request_headers(raw_headers)
                resp_body = await self._rust_http_client.post(
                    url=uri,
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

        resp = json_decoder.decode(resp_body.decode("utf-8"))

        if not isinstance(resp, dict):
            raise ValueError(
                "The introspection endpoint returned an invalid JSON response."
            )

        # We had a valid response, so we can cache it
        cache_context.should_cache = True
        return IntrospectionResult(
            IntrospectionToken(**resp), retrieved_at_ms=self._clock.time_msec()
        )

    async def is_server_admin(self, requester: Requester) -> bool:
        return "urn:synapse:admin:*" in requester.scope

    def _is_access_token_the_admin_token(self, token: str) -> bool:
        admin_token = self._admin_token()
        if admin_token is None:
            return False
        return token == admin_token

    async def get_user_by_req(
        self,
        request: SynapseRequest,
        allow_guest: bool = False,
        allow_expired: bool = False,
        allow_locked: bool = False,
    ) -> Requester:
        """Get a registered user's ID.

        Args:
            request: An HTTP request with an access_token query parameter.
            allow_guest: If False, will raise an AuthError if the user making the
                request is a guest.
            allow_expired: If True, allow the request through even if the account
                is expired, or session token lifetime has ended. Note that
                /login will deliver access tokens regardless of expiration.

        Returns:
            Resolves to the requester
        Raises:
            InvalidClientCredentialsError if no user by that token exists or the token
                is invalid.
            AuthError if access is denied for the user in the access token
        """
        parent_span = active_span()
        with start_active_span("get_user_by_req"):
            requester = await self._wrapped_get_user_by_req(
                request, allow_guest, allow_expired, allow_locked
            )

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

    async def _wrapped_get_user_by_req(
        self,
        request: SynapseRequest,
        allow_guest: bool = False,
        allow_expired: bool = False,
        allow_locked: bool = False,
    ) -> Requester:
        access_token = self.get_access_token_from_request(request)

        requester = await self.get_appservice_user(request, access_token)
        if not requester:
            # TODO: we probably want to assert the allow_guest inside this call
            # so that we don't provision the user if they don't have enough permission:
            requester = await self.get_user_by_access_token(access_token, allow_expired)

        # Do not record requests from MAS using the virtual `__oidc_admin` user.
        if not self._is_access_token_the_admin_token(access_token):
            await self._record_request(request, requester)

        request.requester = requester

        return requester

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

    def is_request_using_the_admin_token(self, request: SynapseRequest) -> bool:
        """
        Check if the request is using the admin token.

        Args:
            request: The request to check.

        Returns:
            True if the request is using the admin token, False otherwise.
        """
        access_token = self.get_access_token_from_request(request)
        return self._is_access_token_the_admin_token(access_token)

    async def get_user_by_access_token(
        self,
        token: str,
        allow_expired: bool = False,
    ) -> Requester:
        if self._is_access_token_the_admin_token(token):
            # XXX: This is a temporary solution so that the admin API can be called by
            # the OIDC provider. This will be removed once we have OIDC client
            # credentials grant support in matrix-authentication-service.
            logger.info("Admin token used")
            # XXX: that user doesn't exist and won't be provisioned.
            # This is mostly fine for admin calls, but we should also think about doing
            # requesters without a user_id.
            admin_user = UserID("__oidc_admin", self._hostname)
            return create_requester(
                user_id=admin_user,
                scope=["urn:synapse:admin:*"],
            )

        try:
            introspection_result = await self._introspection_cache.wrap(
                token, self._introspect_token, token, cache_context=True
            )
        except Exception:
            logger.exception("Failed to introspect token")
            raise SynapseError(503, "Unable to introspect the access token")

        logger.debug("Introspection result: %r", introspection_result)

        # TODO: introspection verification should be more extensive, especially:
        #   - verify the audience
        if not introspection_result.is_active(self._clock.time_msec()):
            raise InvalidClientTokenError("Token is not active")

        # Let's look at the scope
        scope: list[str] = introspection_result.get_scope_list()

        # Determine type of user based on presence of particular scopes
        has_user_scope = (
            UNSTABLE_SCOPE_MATRIX_API in scope or STABLE_SCOPE_MATRIX_API in scope
        )

        if not has_user_scope:
            raise InvalidClientTokenError("No scope in token granting user rights")

        # Match via the sub claim
        sub = introspection_result.get_sub()
        if sub is None:
            raise InvalidClientTokenError(
                "Invalid sub claim in the introspection result"
            )

        user_id_str = await self.store.get_user_by_external_id(
            MSC3861DelegatedAuth.EXTERNAL_ID_PROVIDER, sub
        )
        if user_id_str is None:
            # If we could not find a user via the external_id, it either does not exist,
            # or the external_id was never recorded

            username = introspection_result.get_username()
            if username is None:
                raise AuthError(
                    500,
                    "Invalid username claim in the introspection result",
                )
            user_id = UserID(username, self._hostname)

            # Try to find a user from the username claim
            user_info = await self.store.get_user_by_id(user_id=user_id.to_string())
            if user_info is None:
                raise AuthError(
                    500,
                    "User not found",
                )

            # And record the sub as external_id
            await self.store.record_user_external_id(
                MSC3861DelegatedAuth.EXTERNAL_ID_PROVIDER, sub, user_id.to_string()
            )
        else:
            user_id = UserID.from_string(user_id_str)

        # MAS 0.15+ will give us the device ID as an explicit value for compatibility sessions
        # If present, we get it from here, if not we get it in thee scope
        device_id = introspection_result.get_device_id()
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

            # Make sure the device exists
            await self.store.get_device(
                user_id=user_id.to_string(), device_id=device_id
            )

        # TODO: there is a few things missing in the requester here, which still need
        # to be figured out, like:
        #   - impersonation, with the `authenticated_entity`, which is used for
        #     rate-limiting, MAU limits, etc.
        #   - shadow-banning, with the `shadow_banned` flag
        #   - a proper solution for appservices, which still needs to be figured out in
        #     the context of MSC3861
        return create_requester(
            user_id=user_id,
            device_id=device_id,
            scope=scope,
        )
