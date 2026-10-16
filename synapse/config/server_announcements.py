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

from typing import Any

import attr

from synapse.config.workers import MAIN_PROCESS_INSTANCE_NAME
from synapse.types import JsonDict, UserID
from synapse.util.stringutils import parse_and_validate_mxc_uri

from ._base import Config, ConfigError


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ServerAnnouncementRoom:
    id: str
    room_name: str
    room_topic: str | None
    room_avatar_url: str | None
    room_avatar_path: str | None
    allowed_user_ids: list[str]


class ServerAnnouncementsConfig(Config):
    section = "serverannouncements"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        server_name = self.root.server.server_name
        # Rooms need an internal creator even when nobody is allowed to send announcements.
        self.server_announcements_mxid = config.get(
            "server_announcements_mxid",
            UserID("_server_announcements", server_name).to_string(),
        )
        if not isinstance(
            self.server_announcements_mxid, str
        ) or not UserID.is_valid_strict(self.server_announcements_mxid):
            raise ConfigError(
                "Must be a valid Matrix user ID", ("server_announcements_mxid",)
            )
        if UserID.from_string(self.server_announcements_mxid).domain != server_name:
            raise ConfigError("Must be a local user", ("server_announcements_mxid",))
        self.rooms: list[ServerAnnouncementRoom] = []

        # The media config may return early when media is disabled on this instance.
        self.media_instance_name = (
            config.get("media_instance_running_background_jobs")
            or MAIN_PROCESS_INSTANCE_NAME
        )

        rooms = config.get("server_announcements", [])
        if not isinstance(rooms, list):
            raise ConfigError("Must be a list", ("server_announcements",))

        # WorkerConfig is parsed after this section. Match its instance resolution
        # so only the uploader needs access to avatar files and the media repository.
        worker_app = config.get("worker_app")
        if worker_app == "synapse.app.homeserver":
            worker_app = None
        worker_name = config.get("worker_name", worker_app)
        instance_name = worker_name or MAIN_PROCESS_INSTANCE_NAME
        room_unique_identifiers: set[str] = set()
        for i, room in enumerate(rooms):
            config_path = ("server_announcements", f"<item {i}>")
            if not isinstance(room, dict):
                raise ConfigError("Must be a mapping", config_path)

            room_unique_identifier = room.get("id")
            if (
                not isinstance(room_unique_identifier, str)
                or not room_unique_identifier.strip()
            ):
                raise ConfigError("Must be a nonempty string", config_path + ("id",))
            if room_unique_identifier in room_unique_identifiers:
                raise ConfigError(
                    f"Duplicate server announcement id: {room_unique_identifier!r}",
                    config_path + ("id",),
                )
            room_unique_identifiers.add(room_unique_identifier)

            room_name = room.get("room_name")
            if not isinstance(room_name, str) or not room_name.strip():
                raise ConfigError(
                    "Must be a nonempty string", config_path + ("room_name",)
                )

            room_topic = room.get("room_topic")
            if room_topic is not None and not isinstance(room_topic, str):
                raise ConfigError("Must be a string", config_path + ("room_topic",))

            room_avatar_url = room.get("room_avatar_url")
            room_avatar_path = room.get("room_avatar_path")
            if room_avatar_url is not None and room_avatar_path is not None:
                raise ConfigError(
                    "Cannot specify both room_avatar_url and room_avatar_path",
                    config_path,
                )
            if room_avatar_url is not None:
                if not isinstance(room_avatar_url, str):
                    raise ConfigError(
                        "Must be a valid MXC URI", config_path + ("room_avatar_url",)
                    )
                try:
                    parse_and_validate_mxc_uri(room_avatar_url)
                except ValueError as e:
                    raise ConfigError(
                        "Must be a valid MXC URI", config_path + ("room_avatar_url",)
                    ) from e

            if room_avatar_path is not None:
                if (
                    not isinstance(room_avatar_path, str)
                    or not room_avatar_path.strip()
                ):
                    raise ConfigError(
                        "Must be a nonempty string", config_path + ("room_avatar_path",)
                    )
                room_avatar_path = self.abspath(room_avatar_path)
                if (
                    self.media_instance_name != MAIN_PROCESS_INSTANCE_NAME
                    and self.media_instance_name not in config.get("instance_map", {})
                ):
                    raise ConfigError(
                        f"Instance {self.media_instance_name!r} is configured to upload "
                        "server announcement avatars but does not appear in "
                        "`instance_map` config.",
                        ("media_instance_running_background_jobs",),
                    )
                if instance_name == self.media_instance_name:
                    if (
                        config.get("enable_media_repo", True) is False
                        and worker_app != "synapse.app.media_repository"
                    ):
                        raise ConfigError(
                            "room_avatar_path requires enable_media_repo to be true "
                            f"on the selected uploader ({self.media_instance_name!r}). "
                            "To upload on a media worker instead, set "
                            "media_instance_running_background_jobs to its worker_name "
                            "and add it to instance_map.",
                            config_path + ("room_avatar_path",),
                        )
                    try:
                        # Avatar files are binary; the shared read_file helper reads text.
                        with open(room_avatar_path, "rb") as avatar_file:
                            avatar_file.read(1)
                    except (OSError, ValueError) as e:
                        raise ConfigError(
                            f"Error accessing file {room_avatar_path!r}",
                            config_path + ("room_avatar_path",),
                        ) from e

            allowed_user_ids = room.get("allowed_user_ids", [])
            if not isinstance(allowed_user_ids, list):
                raise ConfigError("Must be a list", config_path + ("allowed_user_ids",))
            for j, user_id in enumerate(allowed_user_ids):
                user_config_path = config_path + ("allowed_user_ids", f"<item {j}>")
                if not isinstance(user_id, str) or not UserID.is_valid_strict(user_id):
                    raise ConfigError(
                        "Must be a valid Matrix user ID", user_config_path
                    )
                if UserID.from_string(user_id).domain != server_name:
                    raise ConfigError(
                        "Must be a local user: server announcement rooms are not federated",
                        user_config_path,
                    )
                if user_id == self.server_announcements_mxid:
                    raise ConfigError(
                        "The internal server announcements creator is reserved and "
                        "cannot be included in allowed_user_ids",
                        user_config_path,
                    )

            self.rooms.append(
                ServerAnnouncementRoom(
                    id=room_unique_identifier,
                    room_name=room_name,
                    room_topic=room_topic,
                    room_avatar_url=room_avatar_url,
                    room_avatar_path=room_avatar_path,
                    allowed_user_ids=list(allowed_user_ids),
                )
            )
