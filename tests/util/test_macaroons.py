#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2022 The Matrix.org Foundation C.I.C.
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

from pymacaroons.exceptions import MacaroonVerificationFailedException

from synapse.util.macaroons import MacaroonGenerator, OidcSessionData

from tests.server import get_clock
from tests.unittest import TestCase


class MacaroonGeneratorTestCase(TestCase):
    def setUp(self) -> None:
        self.reactor, hs_clock = get_clock()
        self.macaroon_generator = MacaroonGenerator(hs_clock, "tesths", b"verysecret")
        self.other_macaroon_generator = MacaroonGenerator(
            hs_clock, "tesths", b"anothersecretkey"
        )

    def test_guest_access_token(self) -> None:
        """Test the generation and verification of guest access tokens"""
        token = self.macaroon_generator.generate_guest_access_token("@user:tesths")
        user_id = self.macaroon_generator.verify_guest_token(token)
        self.assertEqual(user_id, "@user:tesths")

        # Raises with another secret key
        with self.assertRaises(MacaroonVerificationFailedException):
            self.other_macaroon_generator.verify_guest_token(token)

        # Check that an old access token without the guest caveat does not work
        macaroon = self.macaroon_generator._generate_base_macaroon("access")
        macaroon.add_first_party_caveat(f"user_id = {user_id}")
        macaroon.add_first_party_caveat("nonce = 0123456789abcdef")
        token = macaroon.serialize()

        with self.assertRaises(MacaroonVerificationFailedException):
            self.macaroon_generator.verify_guest_token(token)

    def test_delete_pusher_token(self) -> None:
        """Test the generation and verification of delete_pusher tokens"""
        token = self.macaroon_generator.generate_delete_pusher_token(
            "@user:tesths", "m.mail", "john@example.com"
        )
        user_id = self.macaroon_generator.verify_delete_pusher_token(
            token, "m.mail", "john@example.com"
        )
        self.assertEqual(user_id, "@user:tesths")

        # Raises with another secret key
        with self.assertRaises(MacaroonVerificationFailedException):
            self.other_macaroon_generator.verify_delete_pusher_token(
                token, "m.mail", "john@example.com"
            )

        # Raises when verifying for another pushkey
        with self.assertRaises(MacaroonVerificationFailedException):
            self.macaroon_generator.verify_delete_pusher_token(
                token, "m.mail", "other@example.com"
            )

        # Raises when verifying for another app_id
        with self.assertRaises(MacaroonVerificationFailedException):
            self.macaroon_generator.verify_delete_pusher_token(
                token, "somethingelse", "john@example.com"
            )

        # Check that an old token without the app_id and pushkey still works
        macaroon = self.macaroon_generator._generate_base_macaroon("delete_pusher")
        macaroon.add_first_party_caveat("user_id = @user:tesths")
        token = macaroon.serialize()
        user_id = self.macaroon_generator.verify_delete_pusher_token(
            token, "m.mail", "john@example.com"
        )
        self.assertEqual(user_id, "@user:tesths")

    def test_oidc_session_token(self) -> None:
        """Test the generation and verification of OIDC session cookies"""
        state = "arandomstate"
        session_data = OidcSessionData(
            idp_id="oidc",
            nonce="nonce",
            client_redirect_url="https://example.com/",
            ui_auth_session_id="",
            code_verifier="",
        )
        token = self.macaroon_generator.generate_oidc_session_token(
            state, session_data, duration_in_ms=2 * 60 * 1000
        ).encode("utf-8")
        info = self.macaroon_generator.verify_oidc_session_token(token, state)
        self.assertEqual(session_data, info)

        # Raises with another secret key
        with self.assertRaises(MacaroonVerificationFailedException):
            self.other_macaroon_generator.verify_oidc_session_token(token, state)

        # Should raise with another state
        with self.assertRaises(MacaroonVerificationFailedException):
            self.macaroon_generator.verify_oidc_session_token(token, "anotherstate")

        # Wait a minute
        self.reactor.pump([60])
        # Shouldn't raise
        self.macaroon_generator.verify_oidc_session_token(token, state)
        # Wait another minute
        self.reactor.pump([60])
        # Should raise since it expired
        with self.assertRaises(MacaroonVerificationFailedException):
            self.macaroon_generator.verify_oidc_session_token(token, state)
