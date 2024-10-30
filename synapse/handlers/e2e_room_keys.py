#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 Matrix.org Foundation C.I.C.
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

import logging
from typing import TYPE_CHECKING, Dict, Optional, cast

from typing_extensions import Literal

from synapse.api.errors import (
    Codes,
    NotFoundError,
    RoomKeysVersionError,
    StoreError,
    SynapseError,
)
from synapse.logging.opentracing import log_kv, trace
from synapse.storage.databases.main.e2e_room_keys import RoomKey
from synapse.types import JsonDict
from synapse.util.async_helpers import ReadWriteLock

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


class E2eRoomKeysHandler:
    """
    Implements an optional realtime backup mechanism for encrypted E2E megolm room keys.
    This gives a way for users to store and recover their megolm keys if they lose all
    their clients. It should also extend easily to future room key mechanisms.
    The actual payload of the encrypted keys is completely opaque to the handler.
    """

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main

        # Used to lock whenever a client is uploading key data.  This prevents collisions
        # between clients trying to upload the details of a new session, given all
        # clients belonging to a user will receive and try to upload a new session at
        # roughly the same time.  Also used to lock out uploads when the key is being
        # changed.
        self._upload_lock = ReadWriteLock()

    @trace
    async def get_room_keys(
        self,
        user_id: str,
        version: str,
        room_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[
        Literal["rooms"], Dict[str, Dict[Literal["sessions"], Dict[str, RoomKey]]]
    ]:
        """Bulk get the E2E room keys for a given backup, optionally filtered to a given
        room, or a given session.
        See EndToEndRoomKeyStore.get_e2e_room_keys for full details.

        Args:
            user_id: the user whose keys we're getting
            version: the version ID of the backup we're getting keys from
            room_id: room ID to get keys for, for None to get keys for all rooms
            session_id: session ID to get keys for, for None to get keys for all
                sessions
        Raises:
            NotFoundError: if the backup version does not exist
        Returns:
            A dict giving the session_data and message metadata for these room keys.
            `{"rooms": {room_id: {"sessions": {session_id: room_key}}}}`
        """

        # we deliberately take the lock to get keys so that changing the version
        # works atomically
        async with self._upload_lock.read(user_id):
            # make sure the backup version exists
            try:
                await self.store.get_e2e_room_keys_version_info(user_id, version)
            except StoreError as e:
                if e.code == 404:
                    raise NotFoundError("Unknown backup version")
                else:
                    raise

            results = await self.store.get_e2e_room_keys(
                user_id, version, room_id, session_id
            )

            log_kv(cast(JsonDict, results))
            return results

    @trace
    async def delete_room_keys(
        self,
        user_id: str,
        version: str,
        room_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> JsonDict:
        """Bulk delete the E2E room keys for a given backup, optionally filtered to a given
        room or a given session.
        See EndToEndRoomKeyStore.delete_e2e_room_keys for full details.

        Args:
            user_id: the user whose backup we're deleting
            version: the version ID of the backup we're deleting
            room_id: room ID to delete keys for, for None to delete keys for all
                rooms
            session_id: session ID to delete keys for, for None to delete keys
                for all sessions
        Raises:
            NotFoundError: if the backup version does not exist
        Returns:
            A dict containing the count and etag for the backup version
        """

        # lock for consistency with uploading
        async with self._upload_lock.write(user_id):
            # make sure the backup version exists
            try:
                version_info = await self.store.get_e2e_room_keys_version_info(
                    user_id, version
                )
            except StoreError as e:
                if e.code == 404:
                    raise NotFoundError("Unknown backup version")
                else:
                    raise

            await self.store.delete_e2e_room_keys(user_id, version, room_id, session_id)

            version_etag = version_info["etag"] + 1
            await self.store.update_e2e_room_keys_version(
                user_id, version, None, version_etag
            )

            count = await self.store.count_e2e_room_keys(user_id, version)
            return {"etag": str(version_etag), "count": count}

    @trace
    async def upload_room_keys(
        self, user_id: str, version: str, room_keys: JsonDict
    ) -> JsonDict:
        """Bulk upload a list of room keys into a given backup version, asserting
        that the given version is the current backup version.  room_keys are merged
        into the current backup as described in RoomKeysServlet.on_PUT().

        Args:
            user_id: the user whose backup we're setting
            version: the version ID of the backup we're updating
            room_keys: a nested dict describing the room_keys we're setting:

        {
            "rooms": {
                "!abc:matrix.org": {
                    "sessions": {
                        "c0ff33": {
                            "first_message_index": 1,
                            "forwarded_count": 1,
                            "is_verified": false,
                            "session_data": "SSBBTSBBIEZJU0gK"
                        }
                    }
                }
            }
        }

        Returns:
            A dict containing the count and etag for the backup version

        Raises:
            NotFoundError: if there are no versions defined
            RoomKeysVersionError: if the uploaded version is not the current version
        """

        # TODO: Validate the JSON to make sure it has the right keys.

        # XXX: perhaps we should use a finer grained lock here?
        async with self._upload_lock.write(user_id):
            # Check that the version we're trying to upload is the current version
            try:
                version_info = await self.store.get_e2e_room_keys_version_info(user_id)
            except StoreError as e:
                if e.code == 404:
                    raise NotFoundError("Version '%s' not found" % (version,))
                else:
                    raise

            if version_info["version"] != version:
                # Check that the version we're trying to upload actually exists
                try:
                    version_info = await self.store.get_e2e_room_keys_version_info(
                        user_id, version
                    )
                    # if we get this far, the version must exist
                    raise RoomKeysVersionError(current_version=version_info["version"])
                except StoreError as e:
                    if e.code == 404:
                        raise NotFoundError("Version '%s' not found" % (version,))
                    else:
                        raise

            # Fetch any existing room keys for the sessions that have been
            # submitted.  Then compare them with the submitted keys.  If the
            # key is new, insert it; if the key should be updated, then update
            # it; otherwise, drop it.
            existing_keys = await self.store.get_e2e_room_keys_multi(
                user_id, version, room_keys["rooms"]
            )
            to_insert = []  # batch the inserts together
            changed = False  # if anything has changed, we need to update the etag
            for room_id, room in room_keys["rooms"].items():
                for session_id, room_key in room["sessions"].items():
                    if not isinstance(room_key["is_verified"], bool):
                        msg = (
                            "is_verified must be a boolean in keys for session %s in"
                            "room %s" % (session_id, room_id)
                        )
                        raise SynapseError(400, msg, Codes.INVALID_PARAM)

                    log_kv(
                        {
                            "message": "Trying to upload room key",
                            "room_id": room_id,
                            "session_id": session_id,
                            "user_id": user_id,
                        }
                    )
                    current_room_key = existing_keys.get(room_id, {}).get(session_id)
                    if current_room_key:
                        if self._should_replace_room_key(current_room_key, room_key):
                            log_kv({"message": "Replacing room key."})
                            logger.debug(
                                "Replacing room key. room=%s session=%s user=%s",
                                room_id,
                                session_id,
                                user_id,
                            )
                            # updates are done one at a time in the DB, so send
                            # updates right away rather than batching them up,
                            # like we do with the inserts
                            await self.store.update_e2e_room_key(
                                user_id, version, room_id, session_id, room_key
                            )
                            changed = True
                        else:
                            log_kv({"message": "Not replacing room_key."})
                            logger.debug(
                                "Not replacing room key. room=%s session=%s user=%s",
                                room_id,
                                session_id,
                                user_id,
                            )
                    else:
                        log_kv(
                            {
                                "message": "Room key not found.",
                                "room_id": room_id,
                                "user_id": user_id,
                            }
                        )
                        log_kv({"message": "Replacing room key."})
                        logger.debug(
                            "Inserting new room key. room=%s session=%s user=%s",
                            room_id,
                            session_id,
                            user_id,
                        )
                        to_insert.append((room_id, session_id, room_key))
                        changed = True

            if len(to_insert):
                await self.store.add_e2e_room_keys(user_id, version, to_insert)

            version_etag = version_info["etag"]
            if changed:
                version_etag = version_etag + 1
                await self.store.update_e2e_room_keys_version(
                    user_id, version, None, version_etag
                )

            count = await self.store.count_e2e_room_keys(user_id, version)
            return {"etag": str(version_etag), "count": count}

    @staticmethod
    def _should_replace_room_key(
        current_room_key: Optional[RoomKey], room_key: RoomKey
    ) -> bool:
        """
        Determine whether to replace a given current_room_key (if any)
        with a newly uploaded room_key backup

        Args:
            current_room_key: Optional, the current room_key dict if any
            room_key : The new room_key dict which may or may not be fit to
                replace the current_room_key

        Returns:
            True if current_room_key should be replaced by room_key in the backup
        """

        if current_room_key:
            # spelt out with if/elifs rather than nested boolean expressions
            # purely for legibility.

            if room_key["is_verified"] and not current_room_key["is_verified"]:
                return True
            elif (
                room_key["first_message_index"]
                < current_room_key["first_message_index"]
            ):
                return True
            elif room_key["forwarded_count"] < current_room_key["forwarded_count"]:
                return True
            else:
                return False
        return True

    @trace
    async def create_version(self, user_id: str, version_info: JsonDict) -> str:
        """Create a new backup version.  This automatically becomes the new
        backup version for the user's keys; previous backups will no longer be
        writeable to.

        Args:
            user_id: the user whose backup version we're creating
            version_info: metadata about the new version being created

        {
            "algorithm": "m.megolm_backup.v1",
            "auth_data": "dGhpcyBzaG91bGQgYWN0dWFsbHkgYmUgZW5jcnlwdGVkIGpzb24K"
        }

        Returns:
            The new version number.
        """

        # TODO: Validate the JSON to make sure it has the right keys.

        # lock everyone out until we've switched version
        async with self._upload_lock.write(user_id):
            new_version = await self.store.create_e2e_room_keys_version(
                user_id, version_info
            )
            return new_version

    async def get_version_info(
        self, user_id: str, version: Optional[str] = None
    ) -> JsonDict:
        """Get the info about a given version of the user's backup

        Args:
            user_id: the user whose current backup version we're querying
            version: Optional; if None gives the most recent version
                otherwise a historical one.
        Raises:
            NotFoundError: if the requested backup version doesn't exist
        Returns:
            A info dict that gives the info about the new version.

        {
            "version": "1234",
            "algorithm": "m.megolm_backup.v1",
            "auth_data": "dGhpcyBzaG91bGQgYWN0dWFsbHkgYmUgZW5jcnlwdGVkIGpzb24K"
        }
        """

        async with self._upload_lock.read(user_id):
            try:
                res = await self.store.get_e2e_room_keys_version_info(user_id, version)
            except StoreError as e:
                if e.code == 404:
                    raise NotFoundError("Unknown backup version")
                else:
                    raise

            res["count"] = await self.store.count_e2e_room_keys(user_id, res["version"])
            res["etag"] = str(res["etag"])
            return res

    @trace
    async def delete_version(self, user_id: str, version: Optional[str] = None) -> None:
        """Deletes a given version of the user's e2e_room_keys backup

        Args:
            user_id: the user whose current backup version we're deleting
            version: Optional. the version ID of the backup version we're deleting
                If missing, we delete the current backup version info.
        Raises:
            NotFoundError: if this backup version doesn't exist
        """

        async with self._upload_lock.write(user_id):
            try:
                await self.store.delete_e2e_room_keys_version(user_id, version)
            except StoreError as e:
                if e.code == 404:
                    raise NotFoundError("Unknown backup version")
                else:
                    raise

    @trace
    async def update_version(
        self, user_id: str, version: str, version_info: JsonDict
    ) -> JsonDict:
        """Update the info about a given version of the user's backup

        Args:
            user_id: the user whose current backup version we're updating
            version: the backup version we're updating
            version_info: the new information about the backup
        Raises:
            NotFoundError: if the requested backup version doesn't exist
        Returns:
            An empty dict.
        """
        if "version" not in version_info:
            version_info["version"] = version
        elif version_info["version"] != version:
            raise SynapseError(
                400, "Version in body does not match", Codes.INVALID_PARAM
            )
        async with self._upload_lock.write(user_id):
            try:
                old_info = await self.store.get_e2e_room_keys_version_info(
                    user_id, version
                )
            except StoreError as e:
                if e.code == 404:
                    raise NotFoundError("Unknown backup version")
                else:
                    raise
            if old_info["algorithm"] != version_info["algorithm"]:
                raise SynapseError(400, "Algorithm does not match", Codes.INVALID_PARAM)

            await self.store.update_e2e_room_keys_version(
                user_id, version, version_info
            )

            return {}
