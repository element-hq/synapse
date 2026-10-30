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
from unittest.mock import AsyncMock, Mock, patch

import attr
from matrix_common.types.mxc_uri import MXCUri
from parameterized import parameterized
from PIL import Image

from twisted.internet.defer import ensureDeferred
from twisted.internet.error import ConnectError

from synapse.api.constants import EventTypes
from synapse.api.errors import SynapseError
from synapse.config.server_announcements import ServerAnnouncementRoom
from synapse.handlers.server_announcements import (
    ServerAnnouncementAvatarManager,
    ServerAnnouncementsHandler,
)
from synapse.http import RequestTimedOutError
from synapse.replication.http.server_announcements import (
    ReplicationServerAnnouncementAvatarRestServlet,
)
from synapse.server import HomeServer
from synapse.types import JsonDict

from tests.replication._base import BaseMultiWorkerStreamTestCase
from tests.server import FakeChannel, make_request
from tests.unittest import HomeserverTestCase, override_config


class ServerAnnouncementAvatarWorkerTestCase(BaseMultiWorkerStreamTestCase):
    needs_threadpool = True

    def setUp(self) -> None:
        # Create an image on disk to act as the room avatar.
        directory = TemporaryDirectory(prefix="synapse-announcement-worker-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.avatar_path = self.directory / "worker-avatar.png"
        self._write_image_with_color("red")

        super().setUp()

        self.worker = self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "media1",
                "enable_media_repo": True,
                "media_store_path": str(self.directory / "media"),
                "server_announcements": [
                    {
                        "id": "news",
                        "room_name": "News",
                        "room_avatar_path": str(self.avatar_path),
                    }
                ],
            },
        )
        self.store = self.hs.get_datastores().main
        self.room = self.hs.config.serverannouncements.rooms[0]
        self.manager = ServerAnnouncementAvatarManager(self.hs)
        self.media_repo = self.worker.get_media_repository()
        self.get_success(
            self.store.store_server_announcement_room("news", "!news:test")
        )

        # Main can neither access the avatar filepath, nor is configured as a media
        # worker.
        self.assertFalse(self.hs.config.media.can_load_media_repo)
        assert self.room.room_avatar_path is not None
        self.assertFalse(Path(self.room.room_avatar_path).exists())

        self.patch(
            self.hs,
            "get_media_repository",
            Mock(side_effect=AssertionError("main must not load media")),
        )
        self.patch(
            self.manager,
            "_read_avatar",
            Mock(side_effect=AssertionError("main must not read avatar files")),
        )

    def default_config(self) -> JsonDict:
        config = super().default_config()
        config.update(
            {
                "enable_media_repo": False,
                "media_instance_running_background_jobs": "media1",
                "worker_replication_secret": "announcement-test-secret",
                "server_announcements": [
                    {
                        "id": "news",
                        "room_name": "News",
                        "room_avatar_path": str(
                            self.directory / "not-mounted-on-main.png"
                        ),
                    }
                ],
            }
        )
        config["instance_map"].update(
            {
                "media1": {"host": "testserv", "port": 9093},
                "media2": {"host": "testserv", "port": 9094},
            }
        )
        return config

    def _write_image_with_color(self, color: str) -> None:
        """Create an image with the given colour. Store it at `self.avatar_path`."""
        with BytesIO() as output, Image.new("RGB", (8, 8), color) as image:
            image.save(output, format="PNG")
            self.content = output.getvalue()
        self.avatar_path.write_bytes(self.content)

    def _get_avatar(self) -> str:
        avatar_url = self.get_success(self.manager.get_avatar_url(self.room))
        assert avatar_url is not None
        return avatar_url

    def _assert_uploaded(self, avatar_url: str) -> None:
        """Assert that a room avatar was uploaded to the media store."""
        mxc = MXCUri.from_str(avatar_url)
        self.assertEqual(mxc.server_name, self.hs.hostname)

        self.assertEqual(
            Path(
                self.media_repo.filepaths.local_media_filepath(mxc.media_id)
            ).read_bytes(),
            self.content,
        )
        self.assertEqual(
            self.get_success(
                self.store.get_server_announcement_avatar(config_id="news")
            ),
            (
                str(self.avatar_path),
                hashlib.sha256(self.content).hexdigest(),
                avatar_url,
            ),
        )

    def _make_replication_request(
        self,
        announcement_id: str = "news",
        content: JsonDict | None = None,
        secret: str | None = "announcement-test-secret",
        target: HomeServer | None = None,
    ) -> FakeChannel:
        return make_request(
            self.reactor,
            self._hs_to_site[target or self.worker],
            "POST",
            f"/_synapse/replication/server_announcement_avatar/{announcement_id}/test-txn",
            content or {},
            shorthand=False,
            custom_headers=[("Authorization", f"Bearer {secret}")] if secret else [],
        )

    def test_upload_and_reuse_existing_file_uri(self) -> None:
        client = self.hs.get_replication_client()

        # Check that a replication request was made to a media worker.
        with patch.object(
            client, "post_json_get_json", wraps=client.post_json_get_json
        ) as request:
            avatar_url = self._get_avatar()
            request.assert_awaited_once()
            uri, payload = request.call_args.args
            self.assertIn("/server_announcement_avatar/news/", uri)
            self.assertEqual(set(payload), {"_INT_STREAM_POS"})

        self._assert_uploaded(avatar_url)

        # Check that another reconciliation doesn't re-upload the avatar, but makes use
        # of the cache instead.
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            # Fetch the media.
            self.assertEqual(self._get_avatar(), avatar_url)

            self.manager = ServerAnnouncementAvatarManager(self.hs)
            self.assertEqual(self._get_avatar(), avatar_url)

            upload.assert_not_called()

    def test_max_upload_size_doesnt_matter(self) -> None:
        # Set the max_upload_size to just below the image's size.
        self.worker.config.media.max_upload_size = len(self.content) - 1

        self._assert_uploaded(self._get_avatar())

    def test_changed_file_is_uploaded_again(self) -> None:
        old_url = self._get_avatar()
        self._write_image_with_color("blue")

        new_url = self._get_avatar()

        # Check that the image was re-uploaded.
        self.assertNotEqual(old_url, new_url)
        self._assert_uploaded(new_url)

    def test_reconciliation_uses_media_worker(self) -> None:
        # Let reconciliation create the actual room and its mapping before upload.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="server_announcement_rooms",
                keyvalues={"id": "news"},
                desc="remove_test_mapping",
            )
        )
        handler = ServerAnnouncementsHandler(self.hs)
        self.get_success(handler.reconcile_rooms())

        # Test that the room avatar was actually set.
        room_id = self.get_success(self.store.get_server_announcement_room("news"))
        assert room_id is not None
        avatar = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state_event(
                room_id, EventTypes.RoomAvatar, ""
            )
        )
        assert avatar is not None
        self._assert_uploaded(avatar.content["url"])

        # Check that another reconciliation doesn't send a new state event.
        self.get_success(handler.reconcile_rooms())
        unchanged = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state_event(
                room_id, EventTypes.RoomAvatar, ""
            )
        )
        assert unchanged is not None
        self.assertEqual(avatar.event_id, unchanged.event_id)

    @parameterized.expand([(None,), ("mxc://remote.example/avatar",)])
    def test_direct_url_does_not_contact_media_worker(
        self, avatar_url: str | None
    ) -> None:
        room = attr.evolve(self.room, room_avatar_path=None, room_avatar_url=avatar_url)
        with patch.object(self.hs, "get_replication_client") as replication:
            self.assertEqual(
                self.get_success(self.manager.get_avatar_url(room)), avatar_url
            )
            replication.assert_not_called()

    def test_unknown_announcement_room_is_rejected(self) -> None:
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            channel = self._make_replication_request(
                "unknown_config_id", {"room_avatar_path": str(self.avatar_path)}
            )

            self.assertEqual(channel.code, 404)
            upload.assert_not_called()

    def test_quarantined_worker_upload_is_not_reused(self) -> None:
        # Create the room and upload its avatar before simulating a restart.
        self.get_success(
            self.store.db_pool.simple_delete(
                table="server_announcement_rooms",
                keyvalues={"id": "news"},
                desc="remove_test_mapping",
            )
        )

        self.get_success(ServerAnnouncementsHandler(self.hs).reconcile_rooms())
        room_id = self.get_success(self.store.get_server_announcement_room("news"))
        assert room_id is not None

        state = self.hs.get_storage_controllers().state
        avatar = self.get_success(
            state.get_current_state_event(room_id, EventTypes.RoomAvatar, "")
        )
        assert avatar is not None
        avatar_url = avatar.content["url"]

        self.get_success(
            self.store.quarantine_media_by_id(
                self.hs.hostname, MXCUri.from_str(avatar_url).media_id, "@admin:test"
            )
        )

        with patch.object(self.media_repo, "create_or_update_content") as upload:
            # The media worker must still reject quarantined media.
            failure = self.get_failure(
                self.manager.get_avatar_url(self.room), SynapseError
            )
            self.assertIn("quarantined", failure.value.msg)

            # Startup reconciliation warns instead of failing or replacing the avatar.
            with self.assertLogs(
                "synapse.handlers.server_announcements", level="WARNING"
            ) as logs:
                self.get_success(ServerAnnouncementsHandler(self.hs).reconcile_rooms())

            self.assertIn("news", logs.output[0])
            self.assertIn("quarantined", logs.output[0])
            upload.assert_not_called()

        unchanged = self.get_success(
            state.get_current_state_event(room_id, EventTypes.RoomAvatar, "")
        )
        assert unchanged is not None
        self.assertEqual(avatar.event_id, unchanged.event_id)
