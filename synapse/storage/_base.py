#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
# Copyright 2014-2016 OpenMarket Ltd
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
from abc import ABCMeta
from typing import TYPE_CHECKING, Any, Collection, Dict, Iterable, Optional, Union

from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    make_in_list_sql_clause,  # noqa: F401
)
from synapse.types import get_domain_from_id
from synapse.util import json_decoder
from synapse.util.caches.descriptors import CachedFunction

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


# some of our subclasses have abstract methods, so we use the ABCMeta metaclass.
class SQLBaseStore(metaclass=ABCMeta):
    """Base class for data stores that holds helper functions.

    Note that multiple instances of this class will exist as there will be one
    per data store (and not one per physical database).
    """

    db_pool: DatabasePool

    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        self.hs = hs
        self._clock = hs.get_clock()
        self.database_engine = database.engine
        self.db_pool = database

        self.external_cached_functions: Dict[str, CachedFunction] = {}

    def process_replication_rows(  # noqa: B027 (no-op by design)
        self,
        stream_name: str,
        instance_name: str,
        token: int,
        rows: Iterable[Any],
    ) -> None:
        """
        Used by storage classes to invalidate caches based on incoming replication data. These
        must not update any ID generators, use `process_replication_position`.
        """

    def process_replication_position(  # noqa: B027 (no-op by design)
        self,
        stream_name: str,
        instance_name: str,
        token: int,
    ) -> None:
        """
        Used by storage classes to advance ID generators based on incoming replication data. This
        is called after process_replication_rows such that caches are invalidated before any token
        positions advance.
        """

    def _invalidate_state_caches(
        self, room_id: str, members_changed: Collection[str]
    ) -> None:
        """Invalidates caches that are based on the current state, but does
        not stream invalidations down replication.

        Args:
            room_id: Room where state changed
            members_changed: The user_ids of members that have changed
        """

        # XXX: If you add something to this function make sure you add it to
        # `_invalidate_state_caches_all` as well.

        # If there were any membership changes, purge the appropriate caches.
        for host in {get_domain_from_id(u) for u in members_changed}:
            self._attempt_to_invalidate_cache("is_host_joined", (room_id, host))
            self._attempt_to_invalidate_cache("is_host_invited", (room_id, host))
        if members_changed:
            self._attempt_to_invalidate_cache("get_users_in_room", (room_id,))
            self._attempt_to_invalidate_cache("get_current_hosts_in_room", (room_id,))
            self._attempt_to_invalidate_cache(
                "get_users_in_room_with_profiles", (room_id,)
            )
            self._attempt_to_invalidate_cache(
                "get_number_joined_users_in_room", (room_id,)
            )
            self._attempt_to_invalidate_cache("get_member_counts", (room_id,))
            self._attempt_to_invalidate_cache("get_local_users_in_room", (room_id,))

            # There's no easy way of invalidating this cache for just the users
            # that have changed, so we just clear the entire thing.
            self._attempt_to_invalidate_cache("does_pair_of_users_share_a_room", None)

        for user_id in members_changed:
            self._attempt_to_invalidate_cache(
                "get_user_in_room_with_profile", (room_id, user_id)
            )
            self._attempt_to_invalidate_cache("get_rooms_for_user", (user_id,))
            self._attempt_to_invalidate_cache(
                "_get_rooms_for_local_user_where_membership_is_inner", (user_id,)
            )
            self._attempt_to_invalidate_cache(
                "get_sliding_sync_rooms_for_user", (user_id,)
            )

        # Purge other caches based on room state.
        self._attempt_to_invalidate_cache("get_room_summary", (room_id,))
        self._attempt_to_invalidate_cache("get_partial_current_state_ids", (room_id,))
        self._attempt_to_invalidate_cache("get_room_type", (room_id,))
        self._attempt_to_invalidate_cache("get_room_encryption", (room_id,))
        self._attempt_to_invalidate_cache("get_sliding_sync_rooms_for_user", None)

    def _invalidate_state_caches_all(self, room_id: str) -> None:
        """Invalidates caches that are based on the current state, but does
        not stream invalidations down replication.

        Same as `_invalidate_state_caches`, except that works when we don't know
        which memberships have changed.

        Args:
            room_id: Room where state changed
        """
        self._attempt_to_invalidate_cache("get_partial_current_state_ids", (room_id,))
        self._attempt_to_invalidate_cache("get_users_in_room", (room_id,))
        self._attempt_to_invalidate_cache("is_host_invited", None)
        self._attempt_to_invalidate_cache("is_host_joined", None)
        self._attempt_to_invalidate_cache("get_current_hosts_in_room", (room_id,))
        self._attempt_to_invalidate_cache("get_users_in_room_with_profiles", (room_id,))
        self._attempt_to_invalidate_cache("get_number_joined_users_in_room", (room_id,))
        self._attempt_to_invalidate_cache("get_member_counts", (room_id,))
        self._attempt_to_invalidate_cache("get_local_users_in_room", (room_id,))
        self._attempt_to_invalidate_cache("does_pair_of_users_share_a_room", None)
        self._attempt_to_invalidate_cache("get_user_in_room_with_profile", None)
        self._attempt_to_invalidate_cache("get_rooms_for_user", None)
        self._attempt_to_invalidate_cache(
            "_get_rooms_for_local_user_where_membership_is_inner", None
        )
        self._attempt_to_invalidate_cache("get_room_summary", (room_id,))
        self._attempt_to_invalidate_cache("get_room_type", (room_id,))
        self._attempt_to_invalidate_cache("get_room_encryption", (room_id,))
        self._attempt_to_invalidate_cache("get_sliding_sync_rooms_for_user", None)

    def _attempt_to_invalidate_cache(
        self, cache_name: str, key: Optional[Collection[Any]]
    ) -> bool:
        """Attempts to invalidate the cache of the given name, ignoring if the
        cache doesn't exist. Mainly used for invalidating caches on workers,
        where they may not have the cache.

        Note that this function does not invalidate any remote caches, only the
        local in-memory ones. Any remote invalidation must be performed before
        calling this.

        Args:
            cache_name
            key: Entry to invalidate. If None then invalidates the entire
                cache.
        """

        try:
            cache = getattr(self, cache_name)
        except AttributeError:
            # Check if an externally defined module cache has been registered
            cache = self.external_cached_functions.get(cache_name)
            if not cache:
                # We probably haven't pulled in the cache in this worker,
                # which is fine.
                return False

        if key is None:
            cache.invalidate_all()
        else:
            # Prefer any local-only invalidation method. Invalidating any non-local
            # cache must be be done before this.
            invalidate_method = getattr(cache, "invalidate_local", cache.invalidate)
            invalidate_method(tuple(key))

        return True

    def register_external_cached_function(
        self, cache_name: str, func: CachedFunction
    ) -> None:
        self.external_cached_functions[cache_name] = func


def db_to_json(db_content: Union[memoryview, bytes, bytearray, str]) -> Any:
    """
    Take some data from a database row and return a JSON-decoded object.

    Args:
        db_content: The JSON-encoded contents from the database.

    Returns:
        The object decoded from JSON.
    """
    # psycopg2 on Python 3 returns memoryview objects, which we need to
    # cast to bytes to decode
    if isinstance(db_content, memoryview):
        db_content = db_content.tobytes()

    # Decode it to a Unicode string before feeding it to the JSON decoder, since
    # it only supports handling strings
    if isinstance(db_content, (bytes, bytearray)):
        db_content = db_content.decode("utf8")

    try:
        return json_decoder.decode(db_content)
    except Exception:
        logging.warning("Tried to decode '%r' as JSON and failed", db_content)
        raise
