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

"""Experimental server announcement configuration.

Tracking issue: https://github.com/element-hq/synapse/issues/20240

Note: The options below live under `experimental_features` while this feature is
incomplete.

## server_announcements

This feature allows homeserver admins to create and manage "announcement" rooms. These
rooms and their events will be visible to all users on the homeserver, except those
listed in the room's `excluded_user_ids`.

Read-only users don't *actually* join announcement rooms, but they will see it in their
room list.

Multiple announcement rooms may be configured for different purposes (server updates,
maintanence warnings, etc.).

### The `rooms` option

A list of server announcement rooms managed by the homeserver. Defaults to an
empty list, which disables the feature. These rooms are not federated, and only
local users can be allowed to send announcements. On startup, the main process
creates missing rooms and reconciles existing rooms with this configuration.

Rooms are invite-only, unencrypted, and have `world_readable` history: their
announcements are public, even though readers cannot join.

On Synapse startup, configured `allowed_user_ids` are automatically invited and joined
to the announcement room. Other users are removed, and outstanding invitations to them
are revoked.

Room state (including name, topic, avatar, power levels, join rules, and history
visibility) is managed by Synapse; attempts to change or redact it through client APIs
are rejected. Senders can leave, but are rejoined on the next reconciliation (which runs
every startup).

Options for each entry:

* `id` (string, required): A unique, nonempty configuration identifier, not a
  Matrix room ID. Do not change it after the associated room has been created.
  It maps the configuration entry to its existing room. Purging the room
  deletes its mapping, allowing the same `id` to map to a new room.
* `room_name` (string, required): The name of the room. Shown publicly to all users.
* `room_topic` (string or null): An optional topic. Defaults to null.
* `room_avatar_url` (string or null): A valid, existing `mxc://` URI for the room
  avatar, which may refer to local or remote media on another homeserver. Mutually
  exclusive with `room_avatar_path`. Defaults to null.
* `room_avatar_path` (string or null): A local file to upload as the room
  avatar. Mutually exclusive with `room_avatar_url`. Defaults to null.

  Uploads run on the main process by default. To use a media worker, set the
  top-level `media_instance_running_background_jobs` to its `worker_name` in
  the shared configuration and add its HTTP replication listener to
  `instance_map`.

  The file's path, content hash, and uploaded MXC URI are stored, so unchanged
  images are not uploaded again on each restart. Replacing the file at the same
  path updates the avatar on the next restart.
* `allowed_user_ids` (array): Full Matrix user IDs allowed to send announcements.
  Each must be a valid, local user; remote users are not supported because these
  rooms are not federated. Defaults to an empty list, allowing no users to send
  announcements. Sender accounts must already exist and must not be deactivated.
* `excluded_user_ids` (array): Full Matrix user IDs excluded from automatically
  receiving announcements from this room, for example appservice users or audit
  bots.

  Each must be a valid, local user. Defaults to an empty list (excluding nobody).

  Accounts do not need to exist yet. Users on this list may still view announcement
  rooms manually (as they are world-readable). But they will not automatically receive
  announcement events.

The internal room manager user (which is used to create announcement rooms and update
their room state) is configured with `room_management_localpart` below. It is a
management identity, not a user-visible sender, and must not be included in
`allowed_user_ids`. Create separate accounts for senders.

Removing an room entry does not delete the room. The room is retained and no longer
reconciled. Re-adding the same `id` resumes management of the room.

An announcement room can be deleted via the admin room-deletion API. If its
configuration entry is kept, Synapse will create a replacement room with the same
logical `id` on next startup. To delete a room permanently, remove both its
configuration entry and purge the room without restarting Synapse in-between.

### The `room_management_localpart` option

The localpart of the user ID used to create and manage announcement rooms.
Defaults to `_server_announcements`.

Use a nonempty string containing only lowercase letters `a-z`, digits `0-9`, or
`_-./=+`. The resulting full user ID (`@<room_management_localpart>:<server_name>`) must
not exceed 255 bytes.

Do not register an account for this identity. Synapse uses it without registration
and refuses to start with announcement rooms configured if an account with this
ID already exists.

This identity stays joined solely to manage rooms, including when `allowed_user_ids`
is empty, and cannot send announcements. It must not appear in `allowed_user_ids`.

This localpart cannot change after a room has been created. To use a different creator,
purge the existing announcement rooms and recreate them with the new value.
Purging deletes their mappings, so the same configuration IDs can be reused.

Example configuration:

    experimental_features:
      server_announcements:
        room_management_localpart: "announcement_manager"
        rooms:
          - id: general
            room_name: Server announcements
            room_topic: News and updates from the server administrators
            room_avatar_url: mxc://example.com/oumMVlgDnLYFaPVkExemNVVZ
            allowed_user_ids:
              - "@alice:example.com"
            excluded_user_ids:
              - "@audit_bot:example.com"

          - id: maintenance
            room_name: Maintenance announcements
            room_avatar_path: /etc/synapse/maintenance-avatar.png
            allowed_user_ids:
              - "@engineer_charlie:example.com"
              - "@full_time_phil:example.com"
"""

from typing import Any

import attr

from synapse.api.constants import MAX_USERID_LENGTH
from synapse.config.workers import MAIN_PROCESS_INSTANCE_NAME
from synapse.types import JsonDict, UserID, contains_invalid_mxid_characters
from synapse.util.stringutils import parse_and_validate_mxc_uri

from ._base import Config, ConfigError


@attr.s(slots=True, frozen=True, auto_attribs=True)
class ServerAnnouncementRoom:
    config_id: str
    room_name: str
    room_topic: str | None
    room_avatar_url: str | None
    room_avatar_path: str | None
    allowed_user_ids: list[str]
    excluded_user_ids: list[str] = attr.ib(factory=list)


class ServerAnnouncementsConfig(Config):
    section = "serverannouncements"

    def read_config(self, config: JsonDict, **kwargs: Any) -> None:
        experimental = config.get("experimental_features") or {}
        announcements = experimental.get("server_announcements", {})
        if not isinstance(announcements, dict):
            raise ConfigError(
                "Must be a mapping; configure rooms under "
                "experimental_features.server_announcements.rooms",
                ("experimental_features", "server_announcements"),
            )
        rooms = announcements.get("rooms", [])
        if not isinstance(rooms, list):
            raise ConfigError(
                "Must be a list",
                ("experimental_features", "server_announcements", "rooms"),
            )

        self.rooms: list[ServerAnnouncementRoom] = []
        # Rooms need an internal creator even when nobody is allowed to send announcements.
        server_name = self.root.server.server_name
        room_management_localpart = announcements.get(
            "room_management_localpart", "_server_announcements"
        )
        self.room_management_mxid = UserID(
            room_management_localpart, server_name
        ).to_string()
        self.media_instance_name = (
            config.get("media_instance_running_background_jobs")
            or MAIN_PROCESS_INSTANCE_NAME
        )

        if rooms:
            if (
                not isinstance(room_management_localpart, str)
                or not room_management_localpart
                or contains_invalid_mxid_characters(room_management_localpart)
            ):
                raise ConfigError(
                    "Must be a valid Matrix user ID localpart, without an '@' prefix "
                    "or server name",
                    (
                        "experimental_features",
                        "server_announcements",
                        "room_management_localpart",
                    ),
                )
            if len(self.room_management_mxid.encode("utf-8")) > MAX_USERID_LENGTH:
                raise ConfigError(
                    f"The management user ID exceeds the {MAX_USERID_LENGTH}-byte limit; "
                    "choose a shorter localpart",
                    (
                        "experimental_features",
                        "server_announcements",
                        "room_management_localpart",
                    ),
                )

        if not rooms:
            return

        # Process each configured room.
        room_unique_identifiers: set[str] = set()
        for room_index, room in enumerate(rooms):
            config_path = (
                "experimental_features",
                "server_announcements",
                "rooms",
                f"<item {room_index}>",
            )
            if not isinstance(room, dict):
                raise ConfigError("Must be a mapping", config_path)

            # Not a room id, but a unique identifier for a room (i.e. "general_announcements")
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

                # Check whether we can upload media.
                worker_app = config.get("worker_app")
                if worker_app == "synapse.app.homeserver":
                    worker_app = None

                worker_name = config.get("worker_name", worker_app)
                instance_name = worker_name or MAIN_PROCESS_INSTANCE_NAME
                is_media_instance = instance_name == self.media_instance_name and (
                    config.get("enable_media_repo", True) is not False
                    or worker_app == "synapse.app.media_repository"
                )

                if is_media_instance:
                    # Verify that the room avatar file actually exists and is accessible
                    # by reading a single byte.
                    #
                    # This is only relevant to worker instances that can upload media.
                    try:
                        # Avatar files are binary; the read_file helper only reads text.
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

            # Verify the configured, allowed users.
            for allowed_user_index, user_id in enumerate(allowed_user_ids):
                user_config_path = config_path + (
                    "allowed_user_ids",
                    f"<item {allowed_user_index}>",
                )
                if not isinstance(user_id, str) or not UserID.is_valid_strict(user_id):
                    raise ConfigError(
                        "Must be a valid Matrix user ID", user_config_path
                    )
                if UserID.from_string(user_id).domain != server_name:
                    raise ConfigError(
                        "Must be a local user: server announcement rooms are not federated",
                        user_config_path,
                    )
                if user_id == self.room_management_mxid:
                    raise ConfigError(
                        "The internal server announcements creator is used to "
                        "create and manage rooms, not send messages in them. It "
                        "cannot be included in allowed_user_ids",
                        user_config_path,
                    )

            excluded_user_ids = room.get("excluded_user_ids", [])
            if not isinstance(excluded_user_ids, list):
                raise ConfigError(
                    "Must be a list", config_path + ("excluded_user_ids",)
                )

            for excluded_user_index, user_id in enumerate(excluded_user_ids):
                user_config_path = config_path + (
                    "excluded_user_ids",
                    f"<item {excluded_user_index}>",
                )
                if not isinstance(user_id, str) or not UserID.is_valid_strict(user_id):
                    raise ConfigError(
                        "Must be a valid Matrix user ID", user_config_path
                    )
                if UserID.from_string(user_id).domain != server_name:
                    raise ConfigError(
                        "Must be a local user: server announcement rooms are not federated",
                        user_config_path,
                    )

            self.rooms.append(
                ServerAnnouncementRoom(
                    config_id=room_unique_identifier,
                    room_name=room_name,
                    room_topic=room_topic,
                    room_avatar_url=room_avatar_url,
                    room_avatar_path=room_avatar_path,
                    allowed_user_ids=list(allowed_user_ids),
                    excluded_user_ids=list(excluded_user_ids),
                )
            )
