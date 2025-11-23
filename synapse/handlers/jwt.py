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
from typing import TYPE_CHECKING

from authlib.jose import JsonWebToken, JWTClaims
from authlib.jose.errors import BadSignatureError, InvalidClaimError, JoseError

from synapse.api.errors import Codes, LoginError
from synapse.types import JsonDict, UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


class JwtHandler:
    def __init__(self, hs: "HomeServer"):
        self.hs = hs

        self.jwt_secret = hs.config.jwt.jwt_secret
        self.jwt_subject_claim = hs.config.jwt.jwt_subject_claim
        self.jwt_display_name_claim = hs.config.jwt.jwt_display_name_claim
        self.jwt_algorithm = hs.config.jwt.jwt_algorithm
        self.jwt_issuer = hs.config.jwt.jwt_issuer
        self.jwt_audiences = hs.config.jwt.jwt_audiences

    def validate_login(self, login_submission: JsonDict) -> tuple[str, str | None]:
        """
        Authenticates the user for the /login API

        Args:
            login_submission: the whole of the login submission
                (including 'type' and other relevant fields)

        Returns:
            A tuple of (user_id, display_name) of the user that is logging in.
            If the JWT does not contain a display name, the second element of the tuple will be None.

        Raises:
            LoginError if there was an authentication problem.
        """
        token = login_submission.get("token", None)
        if token is None:
            raise LoginError(
                403, "Token field for JWT is missing", errcode=Codes.FORBIDDEN
            )

        jwt = JsonWebToken([self.jwt_algorithm])
        claim_options = {}
        if self.jwt_issuer is not None:
            claim_options["iss"] = {"value": self.jwt_issuer, "essential": True}
        if self.jwt_audiences is not None:
            claim_options["aud"] = {"values": self.jwt_audiences, "essential": True}

        try:
            claims = jwt.decode(
                token,
                key=self.jwt_secret,
                claims_cls=JWTClaims,
                claims_options=claim_options,
            )
        except BadSignatureError:
            # We handle this case separately to provide a better error message
            raise LoginError(
                403,
                "JWT validation failed: Signature verification failed",
                errcode=Codes.FORBIDDEN,
            )
        except JoseError as e:
            # A JWT error occurred, return some info back to the client.
            raise LoginError(
                403,
                "JWT validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )

        try:
            claims.validate(leeway=120)  # allows 2 min of clock skew

            # Enforce the old behavior which is rolled out in productive
            # servers: if the JWT contains an 'aud' claim but none is
            # configured, the login attempt will fail
            if claims.get("aud") is not None:
                if self.jwt_audiences is None or len(self.jwt_audiences) == 0:
                    raise InvalidClaimError("aud")
        except JoseError as e:
            raise LoginError(
                403,
                "JWT validation failed: %s" % (str(e),),
                errcode=Codes.FORBIDDEN,
            )

        user = claims.get(self.jwt_subject_claim, None)
        if user is None:
            raise LoginError(403, "Invalid JWT", errcode=Codes.FORBIDDEN)

        default_display_name = None
        if self.jwt_display_name_claim:
            display_name_claim = claims.get(self.jwt_display_name_claim)
            if display_name_claim is not None:
                default_display_name = display_name_claim

        return UserID(user, self.hs.hostname).to_string(), default_display_name
