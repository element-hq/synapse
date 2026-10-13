#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import hashlib
import os
from io import BytesIO
from typing import TYPE_CHECKING

from matrix_common.types.mxc_uri import MXCUri
from PIL import Image

from synapse.config import ConfigError
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.logging.context import defer_to_thread
from synapse.media._base import FileInfo
from synapse.types import UserID

if TYPE_CHECKING:
    from synapse.server import HomeServer


class ServerAnnouncementAvatarManager:
    """Resolve avatars for main-process reconciliation, delegating uploads if needed.

    Only the configured media instance needs access to the avatar file and media
    repository. The caller must persist the announcement's room mapping before
    calling this helper: the uploaded avatar cache belongs to that mapping.
    """

    def __init__(self, hs: "HomeServer") -> None:
        self._hs = hs
        self._store = hs.get_datastores().main

    async def get_avatar_url(self, room: ServerAnnouncementRoom) -> str | None:
        path = room.room_avatar_path
        if path is None:
            # None deliberately clears an old avatar, even if an upload is cached.
            return room.room_avatar_url

        media_instance_name = self._hs.config.serverannouncements.media_instance_name
        if media_instance_name != self._hs.get_instance_name():
            # Import here to avoid a cycle through replication servlet registration.
            from synapse.replication.http.server_announcements import (
                ReplicationServerAnnouncementAvatarRestServlet,
            )

            upload_request = ReplicationServerAnnouncementAvatarRestServlet.make_client(
                self._hs
            )
            response = await upload_request(
                instance_name=media_instance_name, announcement_id=room.id
            )
            return response["avatar_url"]

        if not self._hs.config.media.can_load_media_repo:
            raise self._avatar_error(
                room,
                "The configured avatar uploader must have the media repository enabled",
            )

        # Hash the contents so edits at the same path are detected on startup,
        # even if the file's size and modification time are unchanged.
        content, sha256, media_type = await defer_to_thread(
            self._hs.get_reactor(), self._read_avatar, room, path
        )
        existing_media = await self._store.get_server_announcement_avatar(room.id)
        if existing_media is not None:
            existing_path, existing_hash, existing_mxc = existing_media
            if (
                existing_path == path
                and existing_hash == sha256
                and await self._media_is_available(room, existing_mxc)
            ):
                return existing_mxc

        media_repo = self._hs.get_media_repository()
        with BytesIO(content) as upload:
            mxc = await media_repo.create_or_update_content(
                media_type=media_type,
                upload_name=os.path.basename(path),
                content=upload,
                content_length=len(content),
                auth_user=UserID.from_string(
                    self._hs.config.serverannouncements.server_announcements_mxid
                ),
            )
        avatar_url = str(mxc)
        # Uploads may be automatically quarantined if their hash matches other
        # quarantined media. Do not publish or cache an unusable avatar.
        if not await self._media_is_available(room, avatar_url):
            raise self._avatar_error(
                room, "Uploaded media is missing; check the media storage configuration"
            )
        await self._store.store_server_announcement_avatar(
            room.id, path, sha256, avatar_url
        )
        return avatar_url

    async def _media_is_available(
        self, room: ServerAnnouncementRoom, avatar_url: str
    ) -> bool:
        """Check that a locally uploaded avatar is complete and retrievable.

        A cache entry is reusable only if its local media record and content still
        exist, including content held by a storage provider. Quarantined media
        raises an error rather than being silently re-uploaded.

        This check is only for uploads from room_avatar_path. An administrator's
        explicit room_avatar_url may be remote and is returned without fetching
        or checking its availability.
        """
        try:
            mxc = MXCUri.from_str(avatar_url)
        except ValueError:
            return False
        if not self._hs.is_mine_server_name(mxc.server_name):
            return False

        media = await self._store.get_local_media(mxc.media_id)
        if media is None:
            return False
        if media.quarantined_by is not None:
            raise self._avatar_error(
                room,
                f"Media {avatar_url!r} is quarantined; unquarantine it or configure "
                "a different image",
            )
        if media.media_length is None:
            return False

        # Consult all configured providers, not just the local filesystem, and
        # close the responder even though we do not need to stream its contents.
        responder = await self._hs.get_media_repository().media_storage.fetch_media(
            FileInfo(
                server_name=None, file_id=mxc.media_id, url_cache=bool(media.url_cache)
            )
        )
        if responder is None:
            return False
        with responder:
            return True

    def _read_avatar(
        self, room: ServerAnnouncementRoom, path: str
    ) -> tuple[bytes, str, str]:
        """Read, validate and hash one snapshot of the file, off the reactor thread."""
        try:
            with open(path, "rb") as avatar_file:
                # This administrator-configured file is not subject to client upload limits.
                content = avatar_file.read()
        except (OSError, ValueError) as e:
            raise self._avatar_error(
                room, f"Cannot read file: {e}; check the path and file permissions"
            ) from e

        try:
            with Image.open(BytesIO(content)) as image:
                media_type = Image.MIME.get(image.format or "")
                if media_type is None or not media_type.startswith("image/"):
                    raise ValueError("image format has no supported image MIME type")
                image.verify()
            # verify() alone does not decode pixels for all formats (e.g. JPEG).
            with Image.open(BytesIO(content)) as image:
                image.load()
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as e:
            raise self._avatar_error(
                room, f"Invalid or unsupported image: {e}; provide a valid image file"
            ) from e

        return content, hashlib.sha256(content).hexdigest(), media_type

    @staticmethod
    def _avatar_error(room: ServerAnnouncementRoom, reason: str) -> ConfigError:
        return ConfigError(
            f"Server announcement room {room.id!r}, room_avatar_path "
            f"{room.room_avatar_path!r}: {reason}"
        )
