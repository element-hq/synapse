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

import os
from pathlib import Path
from unittest.mock import patch

import jsonschema
import yaml
from attr.exceptions import FrozenInstanceError
from parameterized import parameterized

from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.config.server_announcements import (
    ServerAnnouncementRoom,
    ServerAnnouncementsConfig,
)
from synapse.config.workers import MAIN_PROCESS_INSTANCE_NAME
from synapse.types import JsonDict

from tests.unittest import TestCase
from tests.utils import default_config

_INVALID_NONEMPTY_STRING_VALUES: tuple[object, ...] = (
    "",
    " \t",
    None,
    42,
    False,
    [],
    {},
)


class ServerAnnouncementsConfigTestCase(TestCase):
    def _read_config(
        self, config_dict: JsonDict, server_name: str = "example.com"
    ) -> ServerAnnouncementsConfig:
        root = HomeServerConfig()
        root.server.server_name = server_name
        config = root.serverannouncements
        config.read_config(config_dict)
        return config

    def _read_room(
        self, room: JsonDict, **config_values: object
    ) -> ServerAnnouncementsConfig:
        return self._read_config(
            {
                "server_announcements": [
                    {"id": "general", "room_name": "Announcements", **room}
                ],
                **config_values,
            }
        )

    @parameterized.expand([("absent", {}), ("empty", {"server_announcements": []})])
    def test_disabled_defaults(self, name: str, config_dict: JsonDict) -> None:
        config = self._read_config(config_dict)
        self.assertEqual(config.rooms, [])
        self.assertEqual(
            config.server_announcements_mxid, "@_server_announcements:example.com"
        )
        self.assertEqual(config.media_instance_name, MAIN_PROCESS_INSTANCE_NAME)

    @parameterized.expand([(None,), ("",), ("master",), ("media1",)])
    def test_media_instance_name(self, instance_name: str | None) -> None:
        config = self._read_config(
            {"media_instance_running_background_jobs": instance_name}
        )
        self.assertEqual(
            config.media_instance_name, instance_name or MAIN_PROCESS_INSTANCE_NAME
        )

    @parameterized.expand(
        [
            ("absent", {}),
            (
                "configured",
                {
                    "server_announcements": [
                        {"id": "general", "room_name": "Announcements"}
                    ]
                },
            ),
            (
                "custom_creator",
                {"server_announcements_mxid": "@announcement_manager:example.com"},
            ),
        ]
    )
    def test_homeserver_config(self, name: str, values: JsonDict) -> None:
        config_dict = default_config(server_name="example.com")
        config_dict.update(values)
        config = HomeServerConfig()
        config.parse_config_dict(config_dict, "", "")
        self.assertIsInstance(config.serverannouncements, ServerAnnouncementsConfig)
        self.assertEqual(
            config.serverannouncements.server_announcements_mxid,
            values.get(
                "server_announcements_mxid", "@_server_announcements:example.com"
            ),
        )
        self.assertEqual(
            len(config.serverannouncements.rooms),
            len(values.get("server_announcements", [])),
        )

    def test_homeserver_config_with_media_worker(self) -> None:
        config_dict = default_config(server_name="example.com")
        config_dict.update(
            {
                "enable_media_repo": False,
                "media_instance_running_background_jobs": "media1",
                "instance_map": {"media1": {"host": "localhost", "port": 9093}},
                "server_announcements": [
                    {
                        "id": "general",
                        "room_name": "Announcements",
                        "room_avatar_path": self.mktemp(),
                    }
                ],
            }
        )
        config = HomeServerConfig()
        with patch("synapse.config.server_announcements.open") as open_file:
            config.parse_config_dict(config_dict, "", "")
        open_file.assert_not_called()
        self.assertFalse(config.media.can_load_media_repo)
        self.assertEqual(config.serverannouncements.media_instance_name, "media1")

    @parameterized.expand(
        [
            (value,)
            for value in _INVALID_NONEMPTY_STRING_VALUES
            + (
                "alice",
                "alice:example.com",
                "#alice:example.com",
                "@alice",
                "@:example.com",
                "@alice:",
                "@a b:example.com",
                "@a\n:example.com",
                "@alice:invalid host",
                "@" + "a" * 244 + ":example.com",
            )
        ]
    )
    def test_invalid_creator(self, value: object) -> None:
        with self.assertRaisesRegex(ConfigError, "valid Matrix user ID") as err:
            self._read_config({"server_announcements_mxid": value})
        self.assertEqual(err.exception.path, ("server_announcements_mxid",))

    @parameterized.expand(
        [
            ("@alice:remote.example.com",),
            ("@alice:example.com.evil",),
            ("@alice:example.com:8448",),
        ]
    )
    def test_remote_creator(self, user_id: str) -> None:
        with self.assertRaisesRegex(ConfigError, "Must be a local user") as err:
            self._read_config({"server_announcements_mxid": user_id})
        self.assertEqual(err.exception.path, ("server_announcements_mxid",))

    def test_room_defaults(self) -> None:
        config = self._read_room({})
        self.assertEqual(
            config.rooms,
            [ServerAnnouncementRoom("general", "Announcements", None, None, None, [])],
        )
        with self.assertRaises(FrozenInstanceError):
            config.rooms[0].room_name = "Changed"  # type: ignore[misc]

    def test_multiple_rooms(self) -> None:
        rooms = [
            {
                "id": "general",
                "room_name": "Announcements",
                "room_topic": "Server news",
                "room_avatar_url": "mxc://media.example.com/avatar",
                "allowed_user_ids": ["@alice:example.com", "@Bob:example.com"],
            },
            {"id": "maintenance", "room_name": "Announcements"},
        ]
        config = self._read_config({"server_announcements": rooms})
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
                ),
                ServerAnnouncementRoom(
                    "maintenance", "Announcements", None, None, None, []
                ),
            ],
        )

    @parameterized.expand([(None,), ({},), ("rooms",), (False,), (42,)])
    def test_invalid_list(self, value: object) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_config({"server_announcements": value})
        self.assertEqual(err.exception.path, ("server_announcements",))

    @parameterized.expand([(None,), ([],), ("room",), (False,), (42,)])
    def test_invalid_entry(self, value: object) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_config({"server_announcements": [value]})
        self.assertEqual(err.exception.path, ("server_announcements", "<item 0>"))

    @parameterized.expand([("id",), ("room_name",)])
    def test_missing_required_field(self, field: str) -> None:
        room = {"id": "general", "room_name": "Announcements"}
        del room[field]
        with self.assertRaises(ConfigError) as err:
            self._read_config({"server_announcements": [room]})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", field)
        )

    @parameterized.expand(
        [
            (field, value)
            for field in ("id", "room_name", "room_avatar_path")
            for value in _INVALID_NONEMPTY_STRING_VALUES
            if field != "room_avatar_path" or value is not None
        ]
    )
    def test_invalid_nonempty_string(self, field: str, value: object) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_room({field: value})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", field)
        )

    def test_duplicate_id(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Duplicate") as err:
            self._read_config(
                {
                    "server_announcements": [
                        {"id": "general", "room_name": "Announcements"},
                        {"id": "general", "room_name": "Other name"},
                    ]
                }
            )
        self.assertEqual(err.exception.path, ("server_announcements", "<item 1>", "id"))

    @parameterized.expand([(None,), ("",), ("Server news",)])
    def test_optional_topic(self, topic: str | None) -> None:
        self.assertEqual(
            self._read_room({"room_topic": topic}).rooms[0].room_topic, topic
        )

    @parameterized.expand([(42,), (False,), ([],), ({},)])
    def test_invalid_topic(self, value: object) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_room({"room_topic": value})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "room_topic")
        )

    @parameterized.expand(
        [
            ("mxc://example.com/avatar",),
            ("mxc://media.example.com:8448/avatar",),
            ("mxc://[::1]:8448/avatar",),
        ]
    )
    def test_avatar_url(self, url: str) -> None:
        room = self._read_room({"room_avatar_url": url}).rooms[0]
        self.assertEqual(room.room_avatar_url, url)
        self.assertIsNone(room.room_avatar_path)

    @parameterized.expand(
        [
            ("",),
            ("https://example.com/avatar.png",),
            ("mxc://example.com",),
            ("mxc://example.com/",),
            ("mxc:///avatar",),
            ("mxc://invalid host/avatar",),
            ("mxc://example.com/avatar/extra",),
            ("mxc://example.com/avatar?query",),
            ("mxc://example.com/avatar#fragment",),
            (42,),
            (False,),
            ([],),
            ({},),
        ]
    )
    def test_invalid_avatar_url(self, value: object) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_room({"room_avatar_url": value})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "room_avatar_url")
        )

    def test_avatar_options_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Cannot specify both"):
            self._read_room(
                {
                    "room_avatar_url": "mxc://example.com/avatar",
                    "room_avatar_path": self.mktemp(),
                }
            )

    def test_null_avatar_options(self) -> None:
        room = self._read_room(
            {"room_avatar_url": None, "room_avatar_path": None}
        ).rooms[0]
        self.assertIsNone(room.room_avatar_url)
        self.assertIsNone(room.room_avatar_path)

    @parameterized.expand(
        [
            ("main", {}),
            ("homeserver_alias", {"worker_app": "synapse.app.homeserver"}),
            (
                "media_worker",
                {
                    "worker_app": "synapse.app.generic_worker",
                    "worker_name": "media1",
                    "media_instance_running_background_jobs": "media1",
                    "instance_map": {"media1": {"host": "localhost", "port": 9093}},
                    "enable_media_repo": True,
                },
            ),
            (
                "legacy_media_worker",
                {
                    "worker_app": "synapse.app.media_repository",
                    "media_instance_running_background_jobs": "synapse.app.media_repository",
                    "instance_map": {
                        "synapse.app.media_repository": {
                            "host": "localhost",
                            "port": 9093,
                        }
                    },
                    "enable_media_repo": False,
                },
            ),
        ]
    )
    def test_avatar_path(self, name: str, config_values: JsonDict) -> None:
        path = self.mktemp()
        with open(path, "wb") as avatar_file:
            avatar_file.write(b"\x89PNG\r\n\x1a\n")
        room = self._read_room(
            {"room_avatar_path": os.path.relpath(path)}, **config_values
        ).rooms[0]
        self.assertEqual(room.room_avatar_path, os.path.abspath(path))
        self.assertIsNone(room.room_avatar_url)

    def test_missing_avatar_file(self) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_room({"room_avatar_path": self.mktemp()})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "room_avatar_path")
        )

    def test_avatar_path_is_directory(self) -> None:
        path = self.mktemp()
        os.mkdir(path)
        with self.assertRaises(ConfigError):
            self._read_room({"room_avatar_path": path})

    def test_unreadable_avatar_file(self) -> None:
        # Mock permissions so this also works when the tests run as root.
        with patch(
            "synapse.config.server_announcements.open", side_effect=PermissionError
        ):
            with self.assertRaises(ConfigError) as err:
                self._read_room({"room_avatar_path": self.mktemp()})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "room_avatar_path")
        )

    @parameterized.expand([(None,), ("synapse.app.homeserver",)])
    def test_avatar_path_requires_media_on_main(self, worker_app: str | None) -> None:
        with self.assertRaisesRegex(ConfigError, "requires enable_media_repo") as err:
            self._read_room(
                {"room_avatar_path": self.mktemp()},
                worker_app=worker_app,
                enable_media_repo=False,
            )
        self.assertIn("media_instance_running_background_jobs", str(err.exception))
        self.assertIn("instance_map", str(err.exception))

    @parameterized.expand(
        [
            ("main", {}),
            ("homeserver_alias", {"worker_app": "synapse.app.homeserver"}),
            ("unnamed_worker", {"worker_app": "synapse.app.generic_worker"}),
            (
                "other_worker",
                {"worker_app": "synapse.app.generic_worker", "worker_name": "worker1"},
            ),
        ]
    )
    def test_other_instances_do_not_need_avatar_file_or_media(
        self, name: str, config_values: JsonDict
    ) -> None:
        path = self.mktemp()
        with patch("synapse.config.server_announcements.open") as open_file:
            config = self._read_room(
                {"room_avatar_path": path},
                media_instance_running_background_jobs="media1",
                instance_map={"media1": {"host": "localhost", "port": 9093}},
                enable_media_repo=False,
                **config_values,
            )
        open_file.assert_not_called()
        self.assertEqual(config.media_instance_name, "media1")
        self.assertEqual(config.rooms[0].room_avatar_path, os.path.abspath(path))

    @parameterized.expand(
        [
            ("unnamed_worker", {"worker_app": "synapse.app.generic_worker"}),
            (
                "named_main",
                {"worker_app": "synapse.app.homeserver", "worker_name": "worker1"},
            ),
        ]
    )
    def test_worker_does_not_need_avatar_file_or_media(
        self, name: str, config_values: JsonDict
    ) -> None:
        with patch("synapse.config.server_announcements.open") as open_file:
            config = self._read_room(
                {"room_avatar_path": self.mktemp()},
                enable_media_repo=False,
                **config_values,
            )
        open_file.assert_not_called()
        self.assertEqual(config.media_instance_name, MAIN_PROCESS_INSTANCE_NAME)

    @parameterized.expand(
        [
            ("named_worker", {"worker_name": "media1"}, "media1"),
            ("default_worker_name", {}, "synapse.app.generic_worker"),
            ("null_worker_name", {"worker_name": None}, "master"),
            ("empty_worker_name", {"worker_name": ""}, "master"),
        ]
    )
    def test_selected_uploader_requires_media(
        self, name: str, config_values: JsonDict, instance_name: str
    ) -> None:
        with self.assertRaisesRegex(ConfigError, "requires enable_media_repo") as err:
            self._read_room(
                {"room_avatar_path": self.mktemp()},
                worker_app="synapse.app.generic_worker",
                media_instance_running_background_jobs=instance_name,
                instance_map={instance_name: {"host": "localhost", "port": 9093}},
                enable_media_repo=False,
                **config_values,
            )
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "room_avatar_path")
        )

    @parameterized.expand(
        [("synapse.app.generic_worker",), ("synapse.app.media_repository",)]
    )
    def test_selected_media_worker_requires_readable_file(
        self, worker_app: str
    ) -> None:
        with self.assertRaisesRegex(ConfigError, "Error accessing file") as err:
            self._read_room(
                {"room_avatar_path": self.mktemp()},
                worker_app=worker_app,
                worker_name="media1",
                media_instance_running_background_jobs="media1",
                instance_map={"media1": {"host": "localhost", "port": 9093}},
            )
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "room_avatar_path")
        )

    def test_media_worker_must_be_in_instance_map(self) -> None:
        with self.assertRaisesRegex(
            ConfigError, "does not appear in `instance_map`"
        ) as err:
            self._read_room(
                {"room_avatar_path": self.mktemp()},
                media_instance_running_background_jobs="media1",
                enable_media_repo=False,
            )
        self.assertEqual(
            err.exception.path, ("media_instance_running_background_jobs",)
        )

    @parameterized.expand(
        [
            ("main", {}),
            ("remote_uploader", {"media_instance_running_background_jobs": "media1"}),
            (
                "selected_uploader",
                {
                    "worker_app": "synapse.app.generic_worker",
                    "worker_name": "media1",
                    "media_instance_running_background_jobs": "media1",
                },
            ),
        ]
    )
    def test_avatar_url_does_not_require_media(
        self, name: str, config_values: JsonDict
    ) -> None:
        url = "mxc://remote.example.com/avatar"
        config = self._read_room(
            {"room_avatar_url": url}, enable_media_repo=False, **config_values
        )
        self.assertEqual(config.rooms[0].room_avatar_url, url)

    @parameterized.expand([(None,), ("@alice:example.com",), ({},), (42,), (False,)])
    def test_invalid_allowlist(self, value: object) -> None:
        with self.assertRaises(ConfigError) as err:
            self._read_room({"allowed_user_ids": value})
        self.assertEqual(
            err.exception.path, ("server_announcements", "<item 0>", "allowed_user_ids")
        )

    @parameterized.expand(
        [
            (None,),
            (42,),
            (False,),
            ([],),
            ({},),
            ("",),
            ("alice",),
            ("alice:example.com",),
            ("#alice:example.com",),
            ("@alice",),
            ("@:example.com",),
            ("@alice:",),
            ("@a b:example.com",),
            ("@a\n:example.com",),
            ("@alice:invalid host",),
            ("@" + "a" * 244 + ":example.com",),
        ]
    )
    def test_invalid_user_id(self, value: object) -> None:
        with self.assertRaisesRegex(ConfigError, "valid Matrix user ID") as err:
            self._read_room({"allowed_user_ids": ["@alice:example.com", value]})
        self.assertEqual(
            err.exception.path,
            ("server_announcements", "<item 0>", "allowed_user_ids", "<item 1>"),
        )

    @parameterized.expand(
        [
            ("@alice:remote.example.com",),
            ("@alice:example.com.evil",),
            ("@alice:example.com:8448",),
        ]
    )
    def test_remote_user(self, user_id: str) -> None:
        with self.assertRaisesRegex(ConfigError, "Must be a local user"):
            self._read_room({"allowed_user_ids": [user_id]})

    @parameterized.expand(
        [
            (server_name, custom_creator)
            for server_name in ("example.com", "example.com:8448", "[::1]:8448")
            for custom_creator in (False, True)
        ]
    )
    def test_local_user_and_creator(
        self, server_name: str, custom_creator: bool
    ) -> None:
        user_id = f"@alice:{server_name}"
        creator = f"@AnnouncementManager:{server_name}"
        config = self._read_config(
            {
                **({"server_announcements_mxid": creator} if custom_creator else {}),
                "server_announcements": [
                    {
                        "id": "general",
                        "room_name": "Announcements",
                        "allowed_user_ids": [user_id],
                    }
                ],
            },
            server_name=server_name,
        )
        self.assertEqual(config.rooms[0].allowed_user_ids, [user_id])
        self.assertEqual(
            config.server_announcements_mxid,
            creator if custom_creator else f"@_server_announcements:{server_name}",
        )

    @parameterized.expand(
        [
            (None,),
            ("@_server_announcements:example.com",),
            ("@announcement_manager:example.com",),
        ]
    )
    def test_reserved_creator(self, creator: str | None) -> None:
        with self.assertRaisesRegex(ConfigError, "creator is reserved") as err:
            self._read_room(
                {"allowed_user_ids": [creator or "@_server_announcements:example.com"]},
                **({"server_announcements_mxid": creator} if creator else {}),
            )
        self.assertEqual(
            err.exception.path,
            ("server_announcements", "<item 0>", "allowed_user_ids", "<item 0>"),
        )

    def test_custom_creator_does_not_reserve_default_creator(self) -> None:
        config = self._read_room(
            {"allowed_user_ids": ["@_server_announcements:example.com"]},
            server_announcements_mxid="@announcement_manager:example.com",
        )
        self.assertEqual(
            config.rooms[0].allowed_user_ids, ["@_server_announcements:example.com"]
        )

    def _read_schema_properties(self) -> JsonDict:
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schema"
            / "synapse-config.schema.yaml"
        )
        with schema_path.open() as schema_file:
            return yaml.safe_load(schema_file)["properties"]

    def test_schema_examples_and_defaults(self) -> None:
        properties = self._read_schema_properties()
        for option in ("server_announcements", "server_announcements_mxid"):
            option_schema = properties[option]
            for example in option_schema["examples"]:
                jsonschema.validate(example, option_schema)
                with patch("synapse.config.server_announcements.open"):
                    self._read_config({option: example})

        config = self._read_config({})
        self.assertEqual(config.rooms, properties["server_announcements"]["default"])
        self.assertEqual(
            config.server_announcements_mxid,
            properties["server_announcements_mxid"]["default"].replace(
                "<server_name>", "example.com"
            ),
        )
        room_defaults = properties["server_announcements"]["items"]["properties"]
        room = self._read_room({}).rooms[0]
        for field in (
            "room_topic",
            "room_avatar_url",
            "room_avatar_path",
            "allowed_user_ids",
        ):
            self.assertEqual(getattr(room, field), room_defaults[field]["default"])

    def test_schema_room_validation(self) -> None:
        schema = self._read_schema_properties()["server_announcements"]
        valid_rooms: list[JsonDict] = [
            {},
            {"room_avatar_url": None, "room_avatar_path": None},
            {
                "room_avatar_url": "mxc://remote.example.com/avatar",
                "room_avatar_path": None,
            },
            {"room_avatar_path": self.mktemp(), "room_avatar_url": None},
        ]
        for room in valid_rooms:
            room = {"id": "general", "room_name": "Announcements", **room}
            jsonschema.validate([room], schema)
            with patch("synapse.config.server_announcements.open"):
                self._read_room(room)

        invalid_rooms: list[JsonDict] = [
            {},
            {"id": "general"},
            {"room_name": "Announcements"},
            {"id": " ", "room_name": "Announcements"},
            {"id": "general", "room_name": "\t"},
            {
                "id": "general",
                "room_name": "Announcements",
                "room_avatar_url": "mxc://example.com/avatar",
                "room_avatar_path": self.mktemp(),
            },
        ]
        for room in invalid_rooms:
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate([room], schema)
            with self.assertRaises(ConfigError):
                self._read_config({"server_announcements": [room]})

    def test_allowlist_is_copied(self) -> None:
        allowed_user_ids = ["@alice:example.com"]
        room = self._read_room({"allowed_user_ids": allowed_user_ids}).rooms[0]
        allowed_user_ids.append("@bob:example.com")
        self.assertEqual(room.allowed_user_ids, ["@alice:example.com"])
