#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 Matrix.org Foundation C.I.C.
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
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    TypeVar,
)
from authlib.jose import JsonWebToken
from typing_extensions import TypedDict
import requests
from synapse.api.errors import Codes, LoginError
from synapse.types import JsonDict, UserID
from synapse.util.caches.cached_call import RetryOnExceptionCachedCall
from authlib.oidc.discovery import get_well_known_url

if TYPE_CHECKING:
    from synapse.server import HomeServer


logger = logging.getLogger(__name__)

JWK = Dict[str, str]
C = TypeVar("C")
#: A JWK Set, as per RFC7517 sec 5.
class JWKS(TypedDict):
    keys: List[JWK]
class CustomJwtHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs
        self.jwt_algorithm = hs.config.custom_jwt.custom_jwt_algorithm
        self.jwt_issuer = hs.config.custom_jwt.custom_jwt_issuer
        self.jwt_audiences = hs.config.custom_jwt.custom_jwt_audiences

        self._http_client = hs.get_proxied_http_client()
        self._jwks = self._load_jwks()

        self._clock = hs.get_clock()

    def _load_jwks(self) -> dict:
        url = get_well_known_url(self.jwt_issuer, external=True)
        jwks_endpoint = requests.get(url).json()['jwks_uri']
        if not jwks_endpoint:
            raise RuntimeError('Missing "jwks_uri" in jwt issuer')
        jwks_response = requests.get(jwks_endpoint)
        jwks_set = jwks_response.json()
        return jwks_set
    def verify_id_token(self,token) -> dict:
        jwt = JsonWebToken(self.jwt_algorithm)

        logger.debug("Attempting to decode JWT %r", token)

        jwk_set = self._jwks
        try:
            claims = jwt.decode(
                token,
                jwk_set,
            )
        except ValueError:
            logger.info("Reloading JWKS after decode error，try reloading the jwks")
            jwk_set = RetryOnExceptionCachedCall(self._load_jwks)
            claims = jwt.decode(
                token,
                key=jwk_set,
            )

        logger.debug("Decoded JWT  %r; validating", claims)
        try:
            claims.validate(
                now=self._clock.time(), leeway=120
            )  # allows 2 min of clock skew
        except jwt.ExpiredSignatureError as e:
            raise LoginError(
                403,
                "custom JWT Token validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )
        return claims

    def validate_login(self, login_submission: JsonDict) -> str:
        token = login_submission.get("token", None)
        if token is None:
            raise LoginError(
                403, "Token field for custom jwt is missing", errcode=Codes.FORBIDDEN
            )

        try:
            decode_token = self.verify_id_token(token)
            user_id = decode_token['sub'].lower()

        except ValueError as e:
        # A JWT error occurred, return some info back to the client.
            raise LoginError(
            403,
                "custom JWT Token validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )
        except Exception as e:
            raise LoginError(
            403,
                "custom JWT Token validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )
    # user = decoded_token['uid']
    # user = "allen666"
        if user_id is None:
            raise LoginError(403, "Invalid custom JWT Token", errcode=Codes.FORBIDDEN)

        return UserID(user_id, self.hs.hostname).to_string()


