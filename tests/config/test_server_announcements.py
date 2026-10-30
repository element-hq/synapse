#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.

from unittest.mock import patch

import yaml
from parameterized import parameterized

from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.config.server_announcements import (
    ServerAnnouncementRoom,
    ServerAnnouncementsConfig,
)
from synapse.types import JsonDict

from tests.unittest import TestCase
from tests.utils import default_config


class ServerAnnouncementsConfigTestCase(TestCase):
    def _read_config(
        self,
        experimental: JsonDict | None = None,
        server_name: str = "example.com",
        **config_values: object,
    ) -> ServerAnnouncementsConfig:
        """Wrap experimental options in experimental_features; kwargs stay top-level."""
        if experimental is not None:
            config_values["experimental_features"] = experimental
        root = HomeServerConfig()
        root.server.server_name = server_name
        config = root.serverannouncements
        config.read_config(config_values)
        return config

    def _read_room(
        self,
        room: JsonDict,
        announcements: JsonDict | None = None,
        *,
        server_name: str = "example.com",
        **config_values: object,
    ) -> ServerAnnouncementsConfig:
        """Given some room data, generates and returns parsed server announcement
        config.
        """
        return self._read_config(
            {
                "server_announcements": {
                    "rooms": [{"id": "general", "room_name": "Announcements", **room}],
                    **(announcements or {}),
                },
            },
            server_name=server_name,
            **config_values,
        )

    @parameterized.expand(
        [
            ("main", {}, False),
            (
                "other_worker",
                {
                    "worker_app": "synapse.app.generic_worker",
                    "worker_name": "worker1",
                    "enable_media_repo": True,
                },
                False,
            ),
            (
                "media_worker",
                {
                    "worker_app": "synapse.app.generic_worker",
                    "worker_name": "media1",
                    "enable_media_repo": True,
                },
                True,
            ),
            (
                "disabled_media_worker",
                {"worker_app": "synapse.app.generic_worker", "worker_name": "media1"},
                False,
            ),
            (
                "legacy_media_worker",
                {"worker_app": "synapse.app.media_repository", "worker_name": "media1"},
                True,
            ),
        ]
    )
    def test_homeserver_config_with_shared_avatar_path(
        self, name: str, config_values: JsonDict, reads_avatar: bool
    ) -> None:
        """Tests that the room avatar file is checked only when we're a media worker.

        Tests that our detection of whether we're a media worker is correct.
        """
        # Set our config.
        config_dict = default_config(server_name="example.com")
        config_dict.update(
            yaml.safe_load(
                """
                enable_media_repo: false
                media_instance_running_background_jobs: media1
                instance_map:
                  main:
                    host: localhost
                    port: 9092
                  media1:
                    host: localhost
                    port: 9093
                experimental_features:
                  server_announcements:
                    rooms:
                      - id: general
                        room_name: Announcements
                        room_avatar_path: /announcements/avatar.png
                """
            )
        )
        config_dict.update(config_values)
        config = HomeServerConfig()

        # Don't actually try to read a file on disk.
        with patch("synapse.config.server_announcements.open") as open_file:
            config.parse_config_dict(config_dict, "", "")

        # This is what we're testing.
        if reads_avatar:
            open_file.assert_called_once_with("/announcements/avatar.png", "rb")
        else:
            open_file.assert_not_called()

        self.assertEqual(
            config.serverannouncements.rooms[0].room_avatar_path,
            "/announcements/avatar.png",
        )

    def test_multiple_rooms(self) -> None:
        rooms = [
            {
                "id": "general",
                "room_name": "Announcements",
                "room_topic": "Server news",
                "room_avatar_url": "mxc://media.example.com/avatar",
                "allowed_user_ids": ["@alice:example.com", "@Bob:example.com"],
                "excluded_user_ids": [
                    "@audit_bot:example.com",
                    "@_appservice:example.com",
                ],
            },
            {"id": "maintenance", "room_name": "Announcements"},
        ]
        config = self._read_config({"server_announcements": {"rooms": rooms}})
        self.assertEqual(
            config.rooms,
            [
                ServerAnnouncementRoom(
                    "general",
                    "Announcements",
                    "Server news",
                    "mxc://media.example.com/avatar",
                    None,
                    ["@alice:example.com", "@Bob:example.com"],
                    ["@audit_bot:example.com", "@_appservice:example.com"],
                ),
                ServerAnnouncementRoom(
                    "maintenance", "Announcements", None, None, None, []
                ),
            ],
        )

    def test_excluded_user_ids_default(self) -> None:
        """Test the defaults of the `excluded_user_ids` option are as expected."""
        self.assertEqual(self._read_room({}).rooms[0].excluded_user_ids, [])
        self.assertEqual(
            self._read_room({"excluded_user_ids": []}).rooms[0].excluded_user_ids, []
        )

    def test_duplicate_id(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Duplicate") as err:
            self._read_config(
                {
                    "server_announcements": {
                        "rooms": [
                            {"id": "general", "room_name": "Announcements"},
                            {"id": "general", "room_name": "Other name"},
                        ]
                    }
                }
            )
        self.assertEqual(
            err.exception.path,
            (
                "experimental_features",
                "server_announcements",
                "rooms",
                "<item 1>",
                "id",
            ),
        )

    def test_avatar_options_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Cannot specify both") as err:
            self._read_room(
                {
                    "room_avatar_url": "mxc://example.com/avatar",
                    "room_avatar_path": self.mktemp(),
                }
            )
        self.assertEqual(
            err.exception.path,
            ("experimental_features", "server_announcements", "rooms", "<item 0>"),
        )

    def test_missing_avatar_file(self) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_room({"room_avatar_path": self.mktemp()})

        self.assertEqual(
            err.exception.path,
            (
                "experimental_features",
                "server_announcements",
                "rooms",
                "<item 0>",
                "room_avatar_path",
            ),
        )
