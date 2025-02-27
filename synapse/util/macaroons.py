#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
# Copyright 2020 Quentin Gliech
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

"""Utilities for manipulating macaroons"""

from typing import Callable, Optional

import attr
import pymacaroons
from pymacaroons.exceptions import MacaroonVerificationFailedException
from typing_extensions import Literal

from synapse.util import Clock, stringutils

MacaroonType = Literal["access", "delete_pusher", "session"]


def get_value_from_macaroon(macaroon: pymacaroons.Macaroon, key: str) -> str:
    """Extracts a caveat value from a macaroon token.

    Checks that there is exactly one caveat of the form "key = <val>" in the macaroon,
    and returns the extracted value.

    Args:
        macaroon: the token
        key: the key of the caveat to extract

    Returns:
        The extracted value

    Raises:
        MacaroonVerificationFailedException: if there are conflicting values for the
             caveat in the macaroon, or if the caveat was not found in the macaroon.
    """
    prefix = key + " = "
    result: Optional[str] = None
    for caveat in macaroon.caveats:
        if not caveat.caveat_id.startswith(prefix):
            continue

        val = caveat.caveat_id[len(prefix) :]

        if result is None:
            # first time we found this caveat: record the value
            result = val
        elif val != result:
            # on subsequent occurrences, raise if the value is different.
            raise MacaroonVerificationFailedException(
                "Conflicting values for caveat " + key
            )

    if result is not None:
        return result

    # If the caveat is not there, we raise a MacaroonVerificationFailedException.
    # Note that it is insecure to generate a macaroon without all the caveats you
    # might need (because there is nothing stopping people from adding extra caveats),
    # so if the caveat isn't there, something odd must be going on.
    raise MacaroonVerificationFailedException("No %s caveat in macaroon" % (key,))


def satisfy_expiry(v: pymacaroons.Verifier, get_time_ms: Callable[[], int]) -> None:
    """Make a macaroon verifier which accepts 'time' caveats

    Builds a caveat verifier which will accept unexpired 'time' caveats, and adds it to
    the given macaroon verifier.

    Args:
        v: the macaroon verifier
        get_time_ms: a callable which will return the timestamp after which the caveat
            should be considered expired. Normally the current time.
    """

    def verify_expiry_caveat(caveat: str) -> bool:
        time_msec = get_time_ms()
        prefix = "time < "
        if not caveat.startswith(prefix):
            return False
        expiry = int(caveat[len(prefix) :])
        return time_msec < expiry

    v.satisfy_general(verify_expiry_caveat)


@attr.s(frozen=True, slots=True, auto_attribs=True)
class OidcSessionData:
    """The attributes which are stored in a OIDC session cookie"""

    idp_id: str
    """The Identity Provider being used"""

    nonce: str
    """The `nonce` parameter passed to the OIDC provider."""

    client_redirect_url: str
    """The URL the client gave when it initiated the flow. ("" if this is a UI Auth)"""

    ui_auth_session_id: str
    """The session ID of the ongoing UI Auth ("" if this is a login)"""

    code_verifier: str
    """The random string used in the RFC7636 code challenge ("" if PKCE is not being used)."""


class MacaroonGenerator:
    def __init__(self, clock: Clock, location: str, secret_key: bytes):
        self._clock = clock
        self._location = location
        self._secret_key = secret_key

    def generate_guest_access_token(self, user_id: str) -> str:
        """Generate a guest access token for the given user ID

        Args:
            user_id: The user ID for which the guest token should be generated.

        Returns:
            A signed access token for that guest user.
        """
        nonce = stringutils.random_string_with_symbols(16)
        macaroon = self._generate_base_macaroon("access")
        macaroon.add_first_party_caveat(f"user_id = {user_id}")
        macaroon.add_first_party_caveat(f"nonce = {nonce}")
        macaroon.add_first_party_caveat("guest = true")
        return macaroon.serialize()

    def generate_delete_pusher_token(
        self, user_id: str, app_id: str, pushkey: str
    ) -> str:
        """Generate a signed token used for unsubscribing from email notifications

        Args:
            user_id: The user for which this token will be valid.
            app_id: The app_id for this pusher.
            pushkey: The unique identifier of this pusher.

        Returns:
            A signed token which can be used in unsubscribe links.
        """
        macaroon = self._generate_base_macaroon("delete_pusher")
        macaroon.add_first_party_caveat(f"user_id = {user_id}")
        macaroon.add_first_party_caveat(f"app_id = {app_id}")
        macaroon.add_first_party_caveat(f"pushkey = {pushkey}")
        return macaroon.serialize()

    def generate_oidc_session_token(
        self,
        state: str,
        session_data: OidcSessionData,
        duration_in_ms: int = (60 * 60 * 1000),
    ) -> str:
        """Generates a signed token storing data about an OIDC session.

        When Synapse initiates an authorization flow, it creates a random state
        and a random nonce. Those parameters are given to the provider and
        should be verified when the client comes back from the provider.
        It is also used to store the client_redirect_url, which is used to
        complete the SSO login flow.

        Args:
            state: The ``state`` parameter passed to the OIDC provider.
            session_data: data to include in the session token.
            duration_in_ms: An optional duration for the token in milliseconds.
                Defaults to an hour.

        Returns:
            A signed macaroon token with the session information.
        """
        now = self._clock.time_msec()
        expiry = now + duration_in_ms
        macaroon = self._generate_base_macaroon("session")
        macaroon.add_first_party_caveat(f"state = {state}")
        macaroon.add_first_party_caveat(f"idp_id = {session_data.idp_id}")
        macaroon.add_first_party_caveat(f"nonce = {session_data.nonce}")
        macaroon.add_first_party_caveat(
            f"client_redirect_url = {session_data.client_redirect_url}"
        )
        macaroon.add_first_party_caveat(
            f"ui_auth_session_id = {session_data.ui_auth_session_id}"
        )
        macaroon.add_first_party_caveat(f"code_verifier = {session_data.code_verifier}")
        macaroon.add_first_party_caveat(f"time < {expiry}")

        return macaroon.serialize()

    def verify_guest_token(self, token: str) -> str:
        """Verify a guest access token macaroon

        Checks that the given token is a valid, unexpired guest access token
        minted by this server.

        Args:
            token: The access token to verify.

        Returns:
            The ``user_id`` that this token is valid for.

        Raises:
            MacaroonVerificationFailedException if the verification failed
        """
        macaroon = pymacaroons.Macaroon.deserialize(token)
        user_id = get_value_from_macaroon(macaroon, "user_id")

        # At some point, Synapse would generate macaroons without the "guest"
        # caveat for regular users. Because of how macaroon verification works,
        # to avoid validating those as guest tokens, we explicitely verify if
        # the macaroon includes the "guest = true" caveat.
        is_guest = any(
            caveat.caveat_id == "guest = true" for caveat in macaroon.caveats
        )

        if not is_guest:
            raise MacaroonVerificationFailedException("Macaroon is not a guest token")

        v = self._base_verifier("access")
        v.satisfy_exact("guest = true")
        v.satisfy_general(lambda c: c.startswith("user_id = "))
        v.satisfy_general(lambda c: c.startswith("nonce = "))
        satisfy_expiry(v, self._clock.time_msec)
        v.verify(macaroon, self._secret_key)

        return user_id

    def verify_delete_pusher_token(self, token: str, app_id: str, pushkey: str) -> str:
        """Verify a token from an email unsubscribe link

        Args:
            token: The token to verify.
            app_id: The app_id of the pusher to delete.
            pushkey: The unique identifier of the pusher to delete.

        Return:
            The ``user_id`` for which this token is valid.

        Raises:
            MacaroonVerificationFailedException if the verification failed
        """
        macaroon = pymacaroons.Macaroon.deserialize(token)
        user_id = get_value_from_macaroon(macaroon, "user_id")

        v = self._base_verifier("delete_pusher")
        v.satisfy_exact(f"app_id = {app_id}")
        v.satisfy_exact(f"pushkey = {pushkey}")
        v.satisfy_general(lambda c: c.startswith("user_id = "))
        v.verify(macaroon, self._secret_key)

        return user_id

    def verify_oidc_session_token(self, session: bytes, state: str) -> OidcSessionData:
        """Verifies and extract an OIDC session token.

        This verifies that a given session token was issued by this homeserver
        and extract the nonce and client_redirect_url caveats.

        Args:
            session: The session token to verify
            state: The state the OIDC provider gave back

        Returns:
            The data extracted from the session cookie

        Raises:
            KeyError if an expected caveat is missing from the macaroon.
        """
        macaroon = pymacaroons.Macaroon.deserialize(session)

        v = self._base_verifier("session")
        v.satisfy_exact(f"state = {state}")
        v.satisfy_general(lambda c: c.startswith("nonce = "))
        v.satisfy_general(lambda c: c.startswith("idp_id = "))
        v.satisfy_general(lambda c: c.startswith("client_redirect_url = "))
        v.satisfy_general(lambda c: c.startswith("ui_auth_session_id = "))
        v.satisfy_general(lambda c: c.startswith("code_verifier = "))
        satisfy_expiry(v, self._clock.time_msec)

        v.verify(macaroon, self._secret_key)

        # Extract the session data from the token.
        nonce = get_value_from_macaroon(macaroon, "nonce")
        idp_id = get_value_from_macaroon(macaroon, "idp_id")
        client_redirect_url = get_value_from_macaroon(macaroon, "client_redirect_url")
        ui_auth_session_id = get_value_from_macaroon(macaroon, "ui_auth_session_id")
        code_verifier = get_value_from_macaroon(macaroon, "code_verifier")
        return OidcSessionData(
            nonce=nonce,
            idp_id=idp_id,
            client_redirect_url=client_redirect_url,
            ui_auth_session_id=ui_auth_session_id,
            code_verifier=code_verifier,
        )

    def _generate_base_macaroon(self, type: MacaroonType) -> pymacaroons.Macaroon:
        macaroon = pymacaroons.Macaroon(
            location=self._location,
            identifier="key",
            key=self._secret_key,
        )
        macaroon.add_first_party_caveat("gen = 1")
        macaroon.add_first_party_caveat(f"type = {type}")
        return macaroon

    def _base_verifier(self, type: MacaroonType) -> pymacaroons.Verifier:
        v = pymacaroons.Verifier()
        v.satisfy_exact("gen = 1")
        v.satisfy_exact(f"type = {type}")
        return v
