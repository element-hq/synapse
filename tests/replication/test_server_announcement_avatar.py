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
from synapse.handlers.server_announcement_avatar import ServerAnnouncementAvatarManager
from synapse.handlers.server_announcements import ServerAnnouncementsHandler
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
        directory = TemporaryDirectory(prefix="synapse-announcement-worker-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.avatar_path = self.directory / "worker-avatar.png"
        self._write_image("red")
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
        # Main has neither a usable avatar path nor a media repository/storage.
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

    def _write_image(self, color: str) -> None:
        with BytesIO() as output, Image.new("RGB", (8, 8), color) as image:
            image.save(output, format="PNG")
            self.content = output.getvalue()
        self.avatar_path.write_bytes(self.content)

    def _get_avatar(self) -> str:
        avatar_url = self.get_success(self.manager.get_avatar_url(self.room))
        assert avatar_url is not None
        return avatar_url

    def _assert_uploaded(self, avatar_url: str) -> None:
        mxc = MXCUri.from_str(avatar_url)
        self.assertEqual(mxc.server_name, self.hs.hostname)
        self.assertEqual(
            Path(
                self.media_repo.filepaths.local_media_filepath(mxc.media_id)
            ).read_bytes(),
            self.content,
        )
        self.assertEqual(
            self.get_success(self.store.get_server_announcement_avatar("news")),
            (
                str(self.avatar_path),
                hashlib.sha256(self.content).hexdigest(),
                avatar_url,
            ),
        )

    def _request(
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
        with patch.object(
            client, "post_json_get_json", wraps=client.post_json_get_json
        ) as request:
            avatar_url = self._get_avatar()
            request.assert_awaited_once()
            uri, payload = request.call_args.args
            self.assertIn("/server_announcement_avatar/news/", uri)
            # Only the logical ID (in the URI) and standard replication metadata
            # cross the process boundary; never main's filesystem path.
            self.assertEqual(set(payload), {"_INT_STREAM_POS"})
        self._assert_uploaded(avatar_url)
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            self.assertEqual(self._get_avatar(), avatar_url)
            # The cache survives replacing the caller, not just an in-memory object.
            self.manager = ServerAnnouncementAvatarManager(self.hs)
            self.assertEqual(self._get_avatar(), avatar_url)
            upload.assert_not_called()

    def test_oversized_worker_avatar_is_allowed(self) -> None:
        self.worker.config.media.max_upload_size = len(self.content) - 1
        self._assert_uploaded(self._get_avatar())

    def test_changed_file_is_uploaded_again(self) -> None:
        old_url = self._get_avatar()
        self._write_image("blue")
        new_url = self._get_avatar()
        self.assertNotEqual(old_url, new_url)
        self._assert_uploaded(new_url)

    def test_main_reconciliation_uses_media_worker(self) -> None:
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
        room_id = self.get_success(self.store.get_server_announcement_room("news"))
        assert room_id is not None
        avatar = self.get_success(
            self.hs.get_storage_controllers().state.get_current_state_event(
                room_id, EventTypes.RoomAvatar, ""
            )
        )
        assert avatar is not None
        self._assert_uploaded(avatar.content["url"])
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

    def test_caller_path_cannot_override_worker_configuration(self) -> None:
        channel = self._request(
            content={"room_avatar_path": str(self.directory / "untrusted.png")}
        )
        self.assertEqual(channel.code, 200, channel.json_body)
        self._assert_uploaded(channel.json_body["avatar_url"])

    def test_unknown_announcement_is_rejected(self) -> None:
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            channel = self._request(
                "unknown", {"room_avatar_path": str(self.avatar_path)}
            )
            self.assertEqual(channel.code, 404)
            upload.assert_not_called()

    def test_missing_mapping_is_rejected_before_upload(self) -> None:
        self.get_success(
            self.store.db_pool.simple_delete(
                table="server_announcement_rooms",
                keyvalues={"id": "news"},
                desc="remove_test_mapping",
            )
        )
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            channel = self._request()
            self.assertEqual(channel.code, 400)
            self.assertIn("no room mapping", channel.json_body["error"])
            upload.assert_not_called()

    def test_room_without_local_avatar_is_rejected(self) -> None:
        room = self.worker.config.serverannouncements.rooms[0]
        self.worker.config.serverannouncements.rooms = [
            attr.evolve(room, room_avatar_path=None)
        ]
        channel = self._request()
        self.assertEqual(channel.code, 400)
        self.assertIn("no room_avatar_path", channel.json_body["error"])

    def test_unselected_media_worker_is_rejected(self) -> None:
        other = self.make_worker_hs(
            "synapse.app.generic_worker",
            {
                "worker_name": "media2",
                "enable_media_repo": True,
                "media_store_path": str(self.directory / "other-media"),
            },
        )
        with patch.object(other, "get_media_repository") as repository:
            channel = self._request(target=other)
            self.assertEqual(channel.code, 400)
            self.assertIn("not the configured", channel.json_body["error"])
            repository.assert_not_called()

    def test_media_disabled_on_target_is_rejected(self) -> None:
        self.worker.config.media.can_load_media_repo = False
        channel = self._request()
        self.assertEqual(channel.code, 400)
        self.assertIn("media repository enabled", channel.json_body["error"])

    @parameterized.expand([(None,), ("wrong-secret",)])
    def test_replication_secret_is_required(self, secret: str | None) -> None:
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            channel = self._request(secret=secret)
            # Existing ReplicationEndpoint authentication failures return 500.
            self.assertEqual(channel.code, 500)
            upload.assert_not_called()
        self.assertIsNone(
            self.get_success(self.store.get_server_announcement_avatar("news"))
        )

    def test_worker_file_error_reaches_main(self) -> None:
        self.avatar_path.unlink()
        failure = self.get_failure(self.manager.get_avatar_url(self.room), SynapseError)
        self.assertEqual(failure.value.code, 500)
        self.assertTrue(failure.value.msg.startswith("Server announcement room 'news'"))
        self.assertIn("Cannot read file", failure.value.msg)
        self.assertIn(str(self.avatar_path), failure.value.msg)

    def test_quarantined_worker_upload_is_not_reused(self) -> None:
        avatar_url = self._get_avatar()
        self.get_success(
            self.store.quarantine_media_by_id(
                self.hs.hostname, MXCUri.from_str(avatar_url).media_id, "@admin:test"
            )
        )
        with patch.object(self.media_repo, "create_or_update_content") as upload:
            failure = self.get_failure(
                self.manager.get_avatar_url(self.room), SynapseError
            )
            self.assertIn("quarantined", failure.value.msg)
            upload.assert_not_called()


class ServerAnnouncementAvatarRetryTestCase(HomeserverTestCase):
    @parameterized.expand([(ConnectError,), (RequestTimedOutError,)])
    @override_config(
        {
            "enable_media_repo": False,
            "media_instance_running_background_jobs": "media1",
            "instance_map": {"media1": {"host": "testserv", "port": 9093}},
        }
    )
    def test_network_failures_return_to_reconciler(
        self, error_type: type[Exception]
    ) -> None:
        room = ServerAnnouncementRoom(
            id="news",
            room_name="News",
            room_topic=None,
            room_avatar_url=None,
            room_avatar_path="/not-mounted-on-main/avatar.png",
            allowed_user_ids=[],
        )
        manager = ServerAnnouncementAvatarManager(self.hs)
        with patch.object(
            self.hs.get_replication_client(),
            "post_json_get_json",
            new_callable=AsyncMock,
        ) as request:
            request.side_effect = error_type("media worker unavailable")
            result = ensureDeferred(manager.get_avatar_url(room))
            # Connection errors use the replication endpoint's bounded exponential
            # backoff (1+2+4+8+16+32 seconds); timeouts must not loop indefinitely.
            for _ in range(65):
                self.reactor.advance(1)
            failure = self.get_failure(result, SynapseError)
            self.assertEqual(failure.value.code, 502)
            if error_type is RequestTimedOutError:
                request.assert_awaited_once()
            else:
                self.assertEqual(
                    request.await_count,
                    ReplicationServerAnnouncementAvatarRestServlet.RETRY_ON_CONNECT_ERROR_ATTEMPTS
                    + 2,
                )
