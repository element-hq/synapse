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

import io
from typing import Iterable

from matrix_common.types.mxc_uri import MXCUri

from twisted.internet.testing import MemoryReactor

from synapse.rest import admin
from synapse.rest.client import login, register, room
from synapse.server import HomeServer
from synapse.types import UserID
from synapse.util.clock import Clock
from synapse.util.stringutils import (
    random_string,
)

from tests import unittest
from tests.unittest import override_config


class MediaRetentionTestCase(unittest.HomeserverTestCase):
    ONE_DAY_IN_MS = 24 * 60 * 60 * 1000
    THIRTY_DAYS_IN_MS = 30 * ONE_DAY_IN_MS

    servlets = [
        room.register_servlets,
        login.register_servlets,
        register.register_servlets,
        admin.register_servlets_for_client_rest_resource,
    ]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.remote_server_name = "remote.homeserver"
        self.store = hs.get_datastores().main

        # Create a user to upload media with
        test_user_id = self.register_user("alice", "password")

        # Inject media (recently accessed, old access, never accessed, old access
        # quarantined media) into both the local store and the remote cache, plus
        # one additional local media that is marked as protected from quarantine.
        media_repository = hs.get_media_repository()

        def _create_media_and_set_attributes(
            last_accessed_ms: int | None,
            is_quarantined: bool | None = False,
            is_protected: bool | None = False,
        ) -> MXCUri:
            # "Upload" some media to the local media store
            # If the meda
            random_content = bytes(random_string(24), "utf-8")
            mxc_uri: MXCUri = self.get_success(
                media_repository.create_or_update_content(
                    media_type="text/plain",
                    upload_name=None,
                    content=io.BytesIO(random_content),
                    content_length=len(random_content),
                    auth_user=UserID.from_string(test_user_id),
                )
            )

            # Set the last recently accessed time for this media
            if last_accessed_ms is not None:
                self.get_success(
                    self.store.update_cached_last_access_time(
                        local_media=(mxc_uri.media_id,),
                        remote_media=(),
                        time_ms=last_accessed_ms,
                    )
                )

            if is_quarantined:
                # Mark this media as quarantined
                self.get_success(
                    self.store.quarantine_media_by_id(
                        server_name=self.hs.config.server.server_name,
                        media_id=mxc_uri.media_id,
                        quarantined_by="@theadmin:test",
                    )
                )

            if is_protected:
                # Mark this media as protected from quarantine
                self.get_success(
                    self.store.mark_local_media_as_safe(
                        media_id=mxc_uri.media_id,
                        safe=True,
                    )
                )

            return mxc_uri

        def _cache_remote_media_and_set_attributes(
            media_id: str,
            last_accessed_ms: int | None,
            is_quarantined: bool | None = False,
        ) -> MXCUri:
            # Pretend to cache some remote media
            self.get_success(
                self.store.store_cached_remote_media(
                    origin=self.remote_server_name,
                    media_id=media_id,
                    media_type="text/plain",
                    media_length=1,
                    time_now_ms=clock.time_msec(),
                    upload_name="testfile.txt",
                    filesystem_id="abcdefg12345",
                    sha256=random_string(24),
                )
            )

            # Set the last recently accessed time for this media
            if last_accessed_ms is not None:
                self.get_success(
                    hs.get_datastores().main.update_cached_last_access_time(
                        local_media=(),
                        remote_media=((self.remote_server_name, media_id),),
                        time_ms=last_accessed_ms,
                    )
                )

            if is_quarantined:
                # Mark this media as quarantined
                self.get_success(
                    self.store.quarantine_media_by_id(
                        server_name=self.remote_server_name,
                        media_id=media_id,
                        quarantined_by="@theadmin:test",
                    )
                )

            return MXCUri(self.remote_server_name, media_id)

        # Start with the local media store
        self.local_recently_accessed_media = _create_media_and_set_attributes(
            last_accessed_ms=self.THIRTY_DAYS_IN_MS,
        )
        self.local_not_recently_accessed_media = _create_media_and_set_attributes(
            last_accessed_ms=self.ONE_DAY_IN_MS,
        )
        self.local_not_recently_accessed_quarantined_media = (
            _create_media_and_set_attributes(
                last_accessed_ms=self.ONE_DAY_IN_MS,
                is_quarantined=True,
            )
        )
        self.local_not_recently_accessed_protected_media = (
            _create_media_and_set_attributes(
                last_accessed_ms=self.ONE_DAY_IN_MS,
                is_protected=True,
            )
        )
        self.local_never_accessed_media = _create_media_and_set_attributes(
            last_accessed_ms=None,
        )

        # And now the remote media store
        self.remote_recently_accessed_media = _cache_remote_media_and_set_attributes(
            media_id="a",
            last_accessed_ms=self.THIRTY_DAYS_IN_MS,
        )
        self.remote_not_recently_accessed_media = (
            _cache_remote_media_and_set_attributes(
                media_id="b",
                last_accessed_ms=self.ONE_DAY_IN_MS,
            )
        )
        self.remote_not_recently_accessed_quarantined_media = (
            _cache_remote_media_and_set_attributes(
                media_id="c",
                last_accessed_ms=self.ONE_DAY_IN_MS,
                is_quarantined=True,
            )
        )
        # Remote media will always have a "last accessed" attribute, as it would not
        # be fetched from the remote homeserver unless instigated by a user.

    @override_config(
        {
            "media_retention": {
                # Enable retention for local media
                "local_media_lifetime": "30d"
                # Cached remote media should not be purged
            }
        }
    )
    def test_local_media_retention(self) -> None:
        """
        Tests that local media that have not been accessed recently is purged, while
        cached remote media is unaffected.
        """
        # Advance 31 days (in seconds)
        self.reactor.advance(31 * 24 * 60 * 60)

        # Check that media has been correctly purged.
        # Local media accessed <30 days ago should still exist.
        # Remote media should be unaffected.
        self._assert_if_mxc_uris_purged(
            purged=[
                self.local_not_recently_accessed_media,
                self.local_never_accessed_media,
            ],
            not_purged=[
                self.local_recently_accessed_media,
                self.local_not_recently_accessed_quarantined_media,
                self.local_not_recently_accessed_protected_media,
                self.remote_recently_accessed_media,
                self.remote_not_recently_accessed_media,
                self.remote_not_recently_accessed_quarantined_media,
            ],
        )

    @override_config(
        {
            "media_retention": {
                # Enable retention for cached remote media
                "remote_media_lifetime": "30d"
                # Local media should not be purged
            }
        }
    )
    def test_remote_media_cache_retention(self) -> None:
        """
        Tests that entries from the remote media cache that have not been accessed
        recently is purged, while local media is unaffected.
        """
        # Advance 31 days (in seconds)
        self.reactor.advance(31 * 24 * 60 * 60)

        # Check that media has been correctly purged.
        # Local media should be unaffected.
        # Remote media accessed <30 days ago should still exist.
        self._assert_if_mxc_uris_purged(
            purged=[
                self.remote_not_recently_accessed_media,
            ],
            not_purged=[
                self.remote_recently_accessed_media,
                self.local_recently_accessed_media,
                self.local_not_recently_accessed_media,
                self.local_not_recently_accessed_quarantined_media,
                self.local_not_recently_accessed_protected_media,
                self.remote_not_recently_accessed_quarantined_media,
                self.local_never_accessed_media,
            ],
        )

    def _assert_if_mxc_uris_purged(
        self, purged: Iterable[MXCUri], not_purged: Iterable[MXCUri]
    ) -> None:
        def _assert_mxc_uri_purge_state(mxc_uri: MXCUri, expect_purged: bool) -> None:
            """Given an MXC URI, assert whether it has been purged or not."""
            if mxc_uri.server_name == self.hs.config.server.server_name:
                found_media = bool(
                    self.get_success(self.store.get_local_media(mxc_uri.media_id))
                )
            else:
                found_media = bool(
                    self.get_success(
                        self.store.get_cached_remote_media(
                            mxc_uri.server_name, mxc_uri.media_id
                        )
                    )
                )

            if expect_purged:
                self.assertFalse(found_media, msg=f"{mxc_uri} unexpectedly not purged")
            else:
                self.assertTrue(
                    found_media,
                    msg=f"{mxc_uri} unexpectedly purged",
                )

        # Assert that the given MXC URIs have either been correctly purged or not.
        for mxc_uri in purged:
            _assert_mxc_uri_purge_state(mxc_uri, expect_purged=True)
        for mxc_uri in not_purged:
            _assert_mxc_uri_purge_state(mxc_uri, expect_purged=False)
