#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from unittest.mock import AsyncMock, patch

from twisted.internet.testing import MemoryReactor

import synapse.rest.admin
from synapse.rest.client import login, voip
from synapse.server import HomeServer
from synapse.util.clock import Clock

from tests import unittest
from tests.unittest import override_config


class ParseCloudflareTurnResponseTestCase(unittest.TestCase):
    """Tests for TURN credential response parsing and validation."""

    def test_validates_turn_broker_credentials(self) -> None:
        """Ensure valid Matrix TURN credentials pass validation."""
        response = voip._validate_turn_credentials_response(
            {
                "username": "user",
                "password": "pass",
                "ttl": 3600,
                "uris": ["turn:turn.example.com:3478?transport=udp"],
            },
            requested_ttl=3600,
        )

        self.assertEqual(
            response,
            {
                "username": "user",
                "password": "pass",
                "ttl": 3600,
                "uris": ["turn:turn.example.com:3478?transport=udp"],
            },
        )

    def test_parses_cloudflare_turn_credentials(self) -> None:
        """Ensure Cloudflare iceServers response is correctly converted to Matrix format."""
        response = voip._parse_cloudflare_turn_response(
            {
                "iceServers": [
                    {"urls": ["stun:stun.cloudflare.com:3478"]},
                    {
                        "urls": [
                            "turn:turn.cloudflare.com:3478?transport=udp",
                            "turns:turn.cloudflare.com:443?transport=tcp",
                        ],
                        "username": "user",
                        "credential": "pass",
                    },
                ]
            },
            ttl=3600,
        )

        self.assertEqual(
            response,
            {
                "username": "user",
                "password": "pass",
                "ttl": 3600,
                "uris": [
                    "turn:turn.cloudflare.com:3478?transport=udp",
                    "turns:turn.cloudflare.com:443?transport=tcp",
                ],
            },
        )

    def test_filters_cloudflare_port_53_turn_urls(self) -> None:
        """Ensure alternate port 53 TURN URLs are filtered out while preserving other ports."""
        response = voip._parse_cloudflare_turn_response(
            {
                "iceServers": [
                    {
                        "urls": [
                            "turn:turn.cloudflare.com:53?transport=udp",
                            "turn:turn.cloudflare.com:3478?transport=udp",
                            "turns:turn.cloudflare.com:443?transport=tcp",
                        ],
                        "username": "user",
                        "credential": "pass",
                    },
                ]
            },
            ttl=3600,
        )

        self.assertEqual(
            response["uris"],
            [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
        )

    def test_preserves_port_5349_turn_urls(self) -> None:
        """Ensure port 5349 (TURNS) is not accidentally filtered by port 53 check."""
        response = voip._parse_cloudflare_turn_response(
            {
                "iceServers": [
                    {
                        "urls": [
                            "turn:turn.cloudflare.com:53?transport=udp",
                            "turn:turn.cloudflare.com:3478?transport=udp",
                            "turns:turn.cloudflare.com:5349?transport=tcp",
                            "turns:turn.cloudflare.com:443?transport=tcp",
                        ],
                        "username": "user",
                        "credential": "pass",
                    },
                ]
            },
            ttl=3600,
        )

        self.assertEqual(
            response["uris"],
            [
                "turn:turn.cloudflare.com:3478?transport=udp",
                "turns:turn.cloudflare.com:5349?transport=tcp",
                "turns:turn.cloudflare.com:443?transport=tcp",
            ],
        )

    def test_rejects_multiple_cloudflare_turn_credentials(self) -> None:
        """Ensure responses with multiple distinct credential sets are rejected."""
        with self.assertRaises(ValueError):
            voip._parse_cloudflare_turn_response(
                {
                    "iceServers": [
                        {
                            "urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
                            "username": "user-a",
                            "credential": "pass-a",
                        },
                        {
                            "urls": ["turns:turn.cloudflare.com:443?transport=tcp"],
                            "username": "user-b",
                            "credential": "pass-b",
                        },
                    ]
                },
                ttl=3600,
            )


class VoipTestCase(unittest.HomeserverTestCase):
    """Integration tests for the VoIP TURN endpoint with different credential modes."""

    servlets = [
        synapse.rest.admin.register_servlets_for_client_rest_resource,
        login.register_servlets,
        voip.register_servlets,
    ]

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        return self.setup_test_homeserver()

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.localpart = "user"
        self.password = "pass"
        self.register_user(self.localpart, self.password)
        self.access_token = self.login(self.localpart, self.password)

    @override_config(
        {
            "turn_mode": "cf",
            "turn_cloudflare_enabled": True,
            "turn_cloudflare_key_id": "cf-key-id",
            "turn_cloudflare_api_token": "cf-api-token",
            "turn_user_lifetime": "1h",
        }
    )
    def test_cloudflare_turn_credentials_are_returned(self) -> None:
        """Ensure Cloudflare credentials are returned when turn_mode is 'cf'."""
        with patch.object(
            voip.VoipRestServlet,
            "_get_cloudflare_turn_credentials",
            new=AsyncMock(
                return_value={
                    "username": "user",
                    "password": "pass",
                    "ttl": 3600,
                    "uris": ["turn:turn.cloudflare.com:3478?transport=udp"],
                }
            ),
        ) as mock_fetch:
            channel = self.make_request(
                "GET", "/voip/turnServer", access_token=self.access_token
            )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "username": "user",
                "password": "pass",
                "ttl": 3600,
                "uris": ["turn:turn.cloudflare.com:3478?transport=udp"],
            },
        )
        self.assertEqual(mock_fetch.await_count, 1)

    @override_config(
        {
            "turn_mode": "broker",
            "turn_federation_deployment": True,
            "turn_broker_url": "https://turn-broker.example.com/credentials",
            "turn_broker_api_token": "broker-token",
            "turn_cloudflare_enabled": True,
            "turn_cloudflare_key_id": "cf-key-id",
            "turn_cloudflare_api_token": "cf-api-token",
            "turn_user_lifetime": "1h",
        }
    )
    def test_federation_deployment_prefers_turn_broker(self) -> None:
        """Ensure broker credentials are preferred over Cloudflare in federation mode."""
        with patch.object(
            voip.VoipRestServlet,
            "_get_turn_broker_credentials",
            new=AsyncMock(
                return_value={
                    "username": "broker-user",
                    "password": "broker-pass",
                    "ttl": 3600,
                    "uris": ["turn:broker.example.com:3478?transport=udp"],
                }
            ),
        ) as mock_broker:
            with patch.object(
                voip.VoipRestServlet,
                "_get_cloudflare_turn_credentials",
                new=AsyncMock(
                    return_value={
                        "username": "cf-user",
                        "password": "cf-pass",
                        "ttl": 3600,
                        "uris": [
                            "turn:turn.cloudflare.com:3478?transport=udp"
                        ],
                    }
                ),
            ) as mock_cloudflare:
                channel = self.make_request(
                    "GET", "/voip/turnServer", access_token=self.access_token
                )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "username": "broker-user",
                "password": "broker-pass",
                "ttl": 3600,
                "uris": ["turn:broker.example.com:3478?transport=udp"],
            },
        )
        self.assertEqual(mock_broker.await_count, 1)
        self.assertEqual(mock_cloudflare.await_count, 0)

    @override_config(
        {
            "turn_mode": "cf",
            "turn_broker_url": "https://turn-broker.example.com/credentials",
            "turn_broker_api_token": "broker-token",
            "turn_cloudflare_enabled": True,
            "turn_cloudflare_key_id": "cf-key-id",
            "turn_cloudflare_api_token": "cf-api-token",
            "turn_user_lifetime": "1h",
        }
    )
    def test_turn_broker_url_is_ignored_without_federation_deployment(self) -> None:
        """Ensure broker settings are ignored when turn_federation_deployment is false."""
        with patch.object(
            voip.VoipRestServlet,
            "_get_turn_broker_credentials",
            new=AsyncMock(
                return_value={
                    "username": "broker-user",
                    "password": "broker-pass",
                    "ttl": 3600,
                    "uris": ["turn:broker.example.com:3478?transport=udp"],
                }
            ),
        ) as mock_broker:
            with patch.object(
                voip.VoipRestServlet,
                "_get_cloudflare_turn_credentials",
                new=AsyncMock(
                    return_value={
                        "username": "cf-user",
                        "password": "cf-pass",
                        "ttl": 3600,
                        "uris": [
                            "turn:turn.cloudflare.com:3478?transport=udp"
                        ],
                    }
                ),
            ) as mock_cloudflare:
                channel = self.make_request(
                    "GET", "/voip/turnServer", access_token=self.access_token
                )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "username": "cf-user",
                "password": "cf-pass",
                "ttl": 3600,
                "uris": ["turn:turn.cloudflare.com:3478?transport=udp"],
            },
        )
        self.assertEqual(mock_broker.await_count, 0)
        self.assertEqual(mock_cloudflare.await_count, 1)

    @override_config(
        {
            "turn_mode": "cf",
            "turn_cloudflare_enabled": True,
            "turn_cloudflare_key_id": "cf-key-id",
            "turn_cloudflare_api_token": "cf-api-token",
            "turn_uris": ["turn:turn.example.com:3478?transport=udp"],
            "turn_username": "fallback-user",
            "turn_password": "fallback-pass",
            "turn_user_lifetime": "1h",
        }
    )
    def test_cloudflare_failure_falls_back_to_existing_turn_config(self) -> None:
        """Ensure local TURN config is used as fallback when Cloudflare fetch fails."""
        with patch.object(
            voip.VoipRestServlet,
            "_get_cloudflare_turn_credentials",
            new=AsyncMock(side_effect=ValueError("boom")),
        ):
            channel = self.make_request(
                "GET", "/voip/turnServer", access_token=self.access_token
            )

        self.assertEqual(channel.code, 200)
        self.assertEqual(
            channel.json_body,
            {
                "username": "fallback-user",
                "password": "fallback-pass",
                "ttl": 3600,
                "uris": ["turn:turn.example.com:3478?transport=udp"],
            },
        )
