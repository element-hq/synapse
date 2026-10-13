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
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import attr
from matrix_common.types.mxc_uri import MXCUri
from parameterized import parameterized
from PIL import Image

from twisted.internet.testing import MemoryReactor

from synapse.api.constants import EventTypes
from synapse.config import ConfigError
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.handlers.server_announcements import (
    ServerAnnouncementAvatarManager,
    ServerAnnouncementsHandler,
)
from synapse.media.storage_provider import (
    FileStorageProviderBackend,
    StorageProviderWrapper,
)
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase


class ServerAnnouncementAvatarIntegrationTestCase(HomeserverTestCase):
    needs_threadpool = True

    def test_reconciliation_uploads_reuses_updates_and_clears_avatar(self) -> None:
        directory = TemporaryDirectory(prefix="synapse-announcement-integration-")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "avatar.png"
        self.hs.config.media.media_store_path = str(Path(directory.name) / "media")
        with Image.new("RGB", (8, 8), "red") as image:
            image.save(path)
        config = self.hs.config.serverannouncements
        config.rooms = [
            ServerAnnouncementRoom(
                config_id="updates",
                room_name="Updates",
                room_topic=None,
                room_avatar_url=None,
                room_avatar_path=str(path),
                allowed_user_ids=[],
            )
        ]
        handler = ServerAnnouncementsHandler(self.hs)
        self.get_success(handler.reconcile_rooms())
        store = self.hs.get_datastores().main
        self.assertIsNone(
            self.get_success(store.get_user_by_id(config.room_management_mxid))
        )
        room_id = self.get_success(store.get_server_announcement_room("updates"))
        assert room_id is not None
        state = self.hs.get_storage_controllers().state
        first = self.get_success(
            state.get_current_state_event(room_id, EventTypes.RoomAvatar, "")
        )
        assert first is not None
        self.assertTrue(first.content["url"].startswith("mxc://test/"))

        handler = ServerAnnouncementsHandler(self.hs)
        self.get_success(handler.reconcile_rooms())
        unchanged = self.get_success(
            state.get_current_state_event(room_id, EventTypes.RoomAvatar, "")
        )
        assert unchanged is not None
        self.assertEqual(first.event_id, unchanged.event_id)
        with Image.new("RGB", (8, 8), "blue") as image:
            image.save(path)
        self.get_success(handler.reconcile_rooms())
        changed = self.get_success(
            state.get_current_state_event(room_id, EventTypes.RoomAvatar, "")
        )
        assert changed is not None
        self.assertNotEqual(first.content["url"], changed.content["url"])

        config.rooms = [attr.evolve(config.rooms[0], room_avatar_path=None)]
        path.unlink()
        self.get_success(handler.reconcile_rooms())
        cleared = self.get_success(
            state.get_current_state_event(room_id, EventTypes.RoomAvatar, "")
        )
        assert cleared is not None
        self.assertEqual(cleared.content, {})
        self.assertEqual(
            self.get_success(store.get_server_announcement_room("updates")), room_id
        )


class ServerAnnouncementAvatarTestCase(HomeserverTestCase):
    needs_threadpool = True

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        directory = TemporaryDirectory(prefix="synapse-announcement-avatar-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        hs.config.media.media_store_path = str(self.directory / "media")
        self.media_repo = hs.get_media_repository()
        self.store = hs.get_datastores().main
        self.uploader = hs.config.serverannouncements.room_management_mxid

        # Deliberately give the PNG a misleading extension.
        self.avatar_path = self.directory / "avatar.jpg"
        self.content = self._image_bytes()
        self.avatar_path.write_bytes(self.content)
        self.room = ServerAnnouncementRoom(
            config_id="news",
            room_name="News",
            room_topic=None,
            room_avatar_url=None,
            room_avatar_path=str(self.avatar_path),
            allowed_user_ids=[],
        )
        self.get_success(
            self.store.store_server_announcement_room(self.room.config_id, "!news:test")
        )
        self.manager = ServerAnnouncementAvatarManager(hs)

    @staticmethod
    def _image_bytes(image_format: str = "PNG", color: str = "red") -> bytes:
        with BytesIO() as output, Image.new("RGB", (8, 8), color) as image:
            image.save(output, format=image_format)
            return output.getvalue()

    def _get_avatar(self) -> str:
        avatar_url = self.get_success(self.manager.get_avatar_url(self.room))
        assert avatar_url is not None
        return avatar_url

    def _assert_cached(self, avatar_url: str) -> None:
        self.assertEqual(
            self.get_success(
                self.store.get_server_announcement_avatar(self.room.config_id)
            ),
            (
                self.room.room_avatar_path,
                hashlib.sha256(self.content).hexdigest(),
                avatar_url,
            ),
        )

    def _assert_stored_content(self, avatar_url: str) -> None:
        mxc = MXCUri.from_str(avatar_url)
        self.assertEqual(mxc.server_name, self.hs.hostname)
        path = Path(self.media_repo.filepaths.local_media_filepath(mxc.media_id))
        self.assertEqual(path.read_bytes(), self.content)

    @parameterized.expand(
        [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif")]
    )
    def test_upload_content_type(self, image_format: str, media_type: str) -> None:
        self.content = self._image_bytes(image_format)
        self.avatar_path.write_bytes(self.content)
        avatar_url = self._get_avatar()
        mxc = MXCUri.from_str(avatar_url)
        media = self.get_success(self.store.get_local_media(mxc.media_id))
        assert media is not None
        self.assertEqual(media.media_type, media_type)
        self.assertEqual(media.media_length, len(self.content))
        self.assertEqual(media.upload_name, self.avatar_path.name)
        self.assertEqual(media.user_id, self.uploader)
        self._assert_stored_content(avatar_url)
        self._assert_cached(avatar_url)

    @parameterized.expand([(None,), ("mxc://elsewhere.example/new-avatar",)])
    def test_removed_path_overrides_cached_upload(self, avatar_url: str | None) -> None:
        old_url = self._get_avatar()
        self.avatar_path.unlink()
        room = attr.evolve(self.room, room_avatar_path=None, room_avatar_url=avatar_url)
        with patch.object(self.hs, "get_media_repository") as get_repository:
            self.assertEqual(
                self.get_success(self.manager.get_avatar_url(room)), avatar_url
            )
            get_repository.assert_not_called()
        self._assert_cached(old_url)

    def test_deleted_cached_media_is_uploaded_again(self) -> None:
        avatar_url = self._get_avatar()
        media_id = MXCUri.from_str(avatar_url).media_id
        self.get_success(self.media_repo.delete_local_media_ids([media_id]))
        self.assertIsNone(self.get_success(self.store.get_local_media(media_id)))
        self.manager = ServerAnnouncementAvatarManager(self.hs)
        new_url = self._get_avatar()
        self.assertNotEqual(new_url, avatar_url)
        self._assert_stored_content(new_url)
        self._assert_cached(new_url)

    def test_missing_stored_file_is_uploaded_again(self) -> None:
        avatar_url = self._get_avatar()
        media_id = MXCUri.from_str(avatar_url).media_id
        Path(self.media_repo.filepaths.local_media_filepath(media_id)).unlink()
        self.assertIsNotNone(self.get_success(self.store.get_local_media(media_id)))
        self.manager = ServerAnnouncementAvatarManager(self.hs)
        new_url = self._get_avatar()
        self.assertNotEqual(new_url, avatar_url)
        self._assert_stored_content(new_url)
        self._assert_cached(new_url)

    def test_cached_media_in_storage_provider_is_reused(self) -> None:
        self.media_repo.media_storage.storage_providers = [
            StorageProviderWrapper(
                FileStorageProviderBackend(self.hs, str(self.directory / "backup")),
                store_local=True,
                store_remote=False,
                store_synchronous=True,
            ),
        ]
        avatar_url = self._get_avatar()
        media_id = MXCUri.from_str(avatar_url).media_id
        Path(self.media_repo.filepaths.local_media_filepath(media_id)).unlink()
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            self.manager = ServerAnnouncementAvatarManager(self.hs)
            self.assertEqual(self._get_avatar(), avatar_url)
            upload.assert_not_called()

    def test_quarantined_cached_media_is_not_used(self) -> None:
        avatar_url = self._get_avatar()
        self.get_success(
            self.store.quarantine_media_by_id(
                self.hs.hostname, MXCUri.from_str(avatar_url).media_id, "@admin:test"
            )
        )
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            self._assert_avatar_error("quarantined")
            upload.assert_not_called()
        self._assert_cached(avatar_url)

    def test_quarantined_upload_is_not_cached(self) -> None:
        self.wait_for_background_updates()
        mxc = self.get_success(
            self.media_repo.create_or_update_content(
                media_type="image/png",
                upload_name="other.png",
                content=BytesIO(self.content),
                content_length=len(self.content),
                auth_user=UserID.from_string(self.uploader),
            )
        )
        self.get_success(
            self.store.quarantine_media_by_id(
                self.hs.hostname, mxc.media_id, "@admin:test"
            )
        )
        self._assert_avatar_error("quarantined")
        self.assertIsNone(
            self.get_success(
                self.store.get_server_announcement_avatar(self.room.config_id)
            )
        )

    def test_oversized_administrator_image_is_allowed(self) -> None:
        self.hs.config.media.max_upload_size = len(self.content) - 1
        self.manager = ServerAnnouncementAvatarManager(self.hs)
        avatar_url = self._get_avatar()
        self._assert_cached(avatar_url)
        self._assert_stored_content(avatar_url)

    @parameterized.expand(
        [
            ("empty", b""),
            ("text", b"not an image"),
            ("truncated_png", b"\x89PNG\r\n\x1a\n"),
        ]
    )
    def test_invalid_image(self, name: str, content: bytes) -> None:
        self.avatar_path.write_bytes(content)
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            self._assert_avatar_error("Invalid or unsupported image")
            upload.assert_not_called()
        self.assertIsNone(
            self.get_success(
                self.store.get_server_announcement_avatar(self.room.config_id)
            )
        )

    def test_invalid_replacement_does_not_use_old_cache(self) -> None:
        avatar_url = self._get_avatar()
        self.avatar_path.write_bytes(b"not an image")
        self._assert_avatar_error("Invalid or unsupported image")
        self._assert_cached(avatar_url)

    def test_missing_avatar_file(self) -> None:
        self.avatar_path.unlink()
        self._assert_avatar_error("Cannot read file")

    def test_unreadable_avatar_file(self) -> None:
        with patch(
            "synapse.handlers.server_announcements.open",
            side_effect=PermissionError("Permission denied"),
        ):
            self._assert_avatar_error("file permissions")

    def _assert_avatar_error(self, message: str) -> None:
        """A test helper to check for the content of avatar config error messages."""
        failure = self.get_failure(self.manager.get_avatar_url(self.room), ConfigError)
        self.assertIn(message, str(failure.value))
        self.assertTrue(
            str(failure.value).startswith(
                f"Server announcement room {self.room.config_id!r},"
            )
        )
        self.assertIn(str(self.avatar_path), str(failure.value))
