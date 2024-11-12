#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019,2020 The Matrix.org Foundation C.I.C.
# Copyright 2016 OpenMarket Ltd
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
from typing import (
    TYPE_CHECKING,
    Any,
    Collection,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    cast,
)

from canonicaljson import encode_canonical_json
from typing_extensions import Literal

from synapse.api.constants import EduTypes
from synapse.api.errors import Codes, StoreError
from synapse.logging.opentracing import (
    get_active_span_text_map,
    set_tag,
    trace,
    whitelisted_homeserver,
)
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.replication.tcp.streams._base import DeviceListsStream
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_comparison_clause,
)
from synapse.storage.databases.main.end_to_end_keys import EndToEndKeyWorkerStore
from synapse.storage.databases.main.roommember import RoomMemberWorkerStore
from synapse.storage.types import Cursor
from synapse.storage.util.id_generators import MultiWriterIdGenerator
from synapse.types import (
    JsonDict,
    JsonMapping,
    StrCollection,
    get_verify_key_from_cross_signing_key,
)
from synapse.util import json_decoder, json_encoder
from synapse.util.caches.descriptors import cached, cachedList
from synapse.util.caches.lrucache import LruCache
from synapse.util.caches.stream_change_cache import StreamChangeCache
from synapse.util.cancellation import cancellable
from synapse.util.iterutils import batch_iter
from synapse.util.stringutils import shortstr

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)
issue_8631_logger = logging.getLogger("synapse.8631_debug")

DROP_DEVICE_LIST_STREAMS_NON_UNIQUE_INDEXES = (
    "drop_device_list_streams_non_unique_indexes"
)

BG_UPDATE_REMOVE_DUP_OUTBOUND_POKES = "remove_dup_outbound_pokes"


class DeviceWorkerStore(RoomMemberWorkerStore, EndToEndKeyWorkerStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        # In the worker store this is an ID tracker which we overwrite in the non-worker
        # class below that is used on the main process.
        self._device_list_id_gen = MultiWriterIdGenerator(
            db_conn=db_conn,
            db=database,
            notifier=hs.get_replication_notifier(),
            stream_name="device_lists_stream",
            instance_name=self._instance_name,
            tables=[
                ("device_lists_stream", "instance_name", "stream_id"),
                ("user_signature_stream", "instance_name", "stream_id"),
                ("device_lists_outbound_pokes", "instance_name", "stream_id"),
                ("device_lists_changes_in_room", "instance_name", "stream_id"),
                ("device_lists_remote_pending", "instance_name", "stream_id"),
                (
                    "device_lists_changes_converted_stream_position",
                    "instance_name",
                    "stream_id",
                ),
            ],
            sequence_name="device_lists_sequence",
            writers=["master"],
        )

        device_list_max = self._device_list_id_gen.get_current_token()
        device_list_prefill, min_device_list_id = self.db_pool.get_cache_dict(
            db_conn,
            "device_lists_stream",
            entity_column="user_id",
            stream_column="stream_id",
            max_value=device_list_max,
            limit=10000,
        )
        self._device_list_stream_cache = StreamChangeCache(
            "DeviceListStreamChangeCache",
            min_device_list_id,
            prefilled_cache=device_list_prefill,
        )

        device_list_room_prefill, min_device_list_room_id = self.db_pool.get_cache_dict(
            db_conn,
            "device_lists_changes_in_room",
            entity_column="room_id",
            stream_column="stream_id",
            max_value=device_list_max,
            limit=10000,
        )
        self._device_list_room_stream_cache = StreamChangeCache(
            "DeviceListRoomStreamChangeCache",
            min_device_list_room_id,
            prefilled_cache=device_list_room_prefill,
        )

        (
            user_signature_stream_prefill,
            user_signature_stream_list_id,
        ) = self.db_pool.get_cache_dict(
            db_conn,
            "user_signature_stream",
            entity_column="from_user_id",
            stream_column="stream_id",
            max_value=device_list_max,
            limit=1000,
        )
        self._user_signature_stream_cache = StreamChangeCache(
            "UserSignatureStreamChangeCache",
            user_signature_stream_list_id,
            prefilled_cache=user_signature_stream_prefill,
        )

        self._device_list_federation_stream_cache = None
        if hs.should_send_federation():
            (
                device_list_federation_prefill,
                device_list_federation_list_id,
            ) = self.db_pool.get_cache_dict(
                db_conn,
                "device_lists_outbound_pokes",
                entity_column="destination",
                stream_column="stream_id",
                max_value=device_list_max,
                limit=10000,
            )
            self._device_list_federation_stream_cache = StreamChangeCache(
                "DeviceListFederationStreamChangeCache",
                device_list_federation_list_id,
                prefilled_cache=device_list_federation_prefill,
            )

        if hs.config.worker.run_background_tasks:
            self._clock.looping_call(
                self._prune_old_outbound_device_pokes, 60 * 60 * 1000
            )

    def process_replication_rows(
        self, stream_name: str, instance_name: str, token: int, rows: Iterable[Any]
    ) -> None:
        if stream_name == DeviceListsStream.NAME:
            self._invalidate_caches_for_devices(token, rows)

        return super().process_replication_rows(stream_name, instance_name, token, rows)

    def process_replication_position(
        self, stream_name: str, instance_name: str, token: int
    ) -> None:
        if stream_name == DeviceListsStream.NAME:
            self._device_list_id_gen.advance(instance_name, token)

        super().process_replication_position(stream_name, instance_name, token)

    def _invalidate_caches_for_devices(
        self, token: int, rows: Iterable[DeviceListsStream.DeviceListsStreamRow]
    ) -> None:
        for row in rows:
            if row.is_signature:
                self._user_signature_stream_cache.entity_has_changed(row.user_id, token)
                continue

            # The entities are either user IDs (starting with '@') whose devices
            # have changed, or remote servers that we need to tell about
            # changes.
            if not row.hosts_calculated:
                self._device_list_stream_cache.entity_has_changed(row.user_id, token)
                self.get_cached_devices_for_user.invalidate((row.user_id,))
                self._get_cached_user_device.invalidate((row.user_id,))
                self.get_device_list_last_stream_id_for_remote.invalidate(
                    (row.user_id,)
                )

    def device_lists_outbound_pokes_have_changed(
        self, destinations: StrCollection, token: int
    ) -> None:
        assert self._device_list_federation_stream_cache is not None

        for destination in destinations:
            self._device_list_federation_stream_cache.entity_has_changed(
                destination, token
            )

    def device_lists_in_rooms_have_changed(
        self, room_ids: StrCollection, token: int
    ) -> None:
        "Record that device lists have changed in rooms"
        for room_id in room_ids:
            self._device_list_room_stream_cache.entity_has_changed(room_id, token)

    def get_device_stream_token(self) -> int:
        return self._device_list_id_gen.get_current_token()

    def get_device_stream_id_generator(self) -> MultiWriterIdGenerator:
        return self._device_list_id_gen

    async def count_devices_by_users(
        self, user_ids: Optional[Collection[str]] = None
    ) -> int:
        """Retrieve number of all devices of given users.
        Only returns number of devices that are not marked as hidden.

        Args:
            user_ids: The IDs of the users which owns devices
        Returns:
            Number of devices of this users.
        """

        def count_devices_by_users_txn(
            txn: LoggingTransaction, user_ids: Collection[str]
        ) -> int:
            sql = """
                SELECT count(*)
                FROM devices
                WHERE
                    hidden = '0' AND
            """

            clause, args = make_in_list_sql_clause(
                txn.database_engine, "user_id", user_ids
            )

            txn.execute(sql + clause, args)
            return cast(Tuple[int], txn.fetchone())[0]

        if not user_ids:
            return 0

        return await self.db_pool.runInteraction(
            "count_devices_by_users", count_devices_by_users_txn, user_ids
        )

    async def get_device(
        self, user_id: str, device_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve a device. Only returns devices that are not marked as
        hidden.

        Args:
            user_id: The ID of the user which owns the device
            device_id: The ID of the device to retrieve
        Returns:
            A dict containing the device information, or `None` if the device does not
            exist.
        """
        row = await self.db_pool.simple_select_one(
            table="devices",
            keyvalues={"user_id": user_id, "device_id": device_id, "hidden": False},
            retcols=("user_id", "device_id", "display_name"),
            desc="get_device",
            allow_none=True,
        )
        if row is None:
            return None
        return {"user_id": row[0], "device_id": row[1], "display_name": row[2]}

    async def get_devices_by_user(
        self, user_id: str
    ) -> Dict[str, Dict[str, Optional[str]]]:
        """Retrieve all of a user's registered devices. Only returns devices
        that are not marked as hidden.

        Args:
            user_id:
        Returns:
            A mapping from device_id to a dict containing "device_id", "user_id"
            and "display_name" for each device. Display name may be null.
        """
        devices = cast(
            List[Tuple[str, str, Optional[str]]],
            await self.db_pool.simple_select_list(
                table="devices",
                keyvalues={"user_id": user_id, "hidden": False},
                retcols=("user_id", "device_id", "display_name"),
                desc="get_devices_by_user",
            ),
        )

        return {
            d[1]: {"user_id": d[0], "device_id": d[1], "display_name": d[2]}
            for d in devices
        }

    async def get_devices_by_auth_provider_session_id(
        self, auth_provider_id: str, auth_provider_session_id: str
    ) -> List[Tuple[str, str]]:
        """Retrieve the list of devices associated with a SSO IdP session ID.

        Args:
            auth_provider_id: The SSO IdP ID as defined in the server config
            auth_provider_session_id: The session ID within the IdP
        Returns:
            A list of dicts containing the device_id and the user_id of each device
        """
        return cast(
            List[Tuple[str, str]],
            await self.db_pool.simple_select_list(
                table="device_auth_providers",
                keyvalues={
                    "auth_provider_id": auth_provider_id,
                    "auth_provider_session_id": auth_provider_session_id,
                },
                retcols=("user_id", "device_id"),
                desc="get_devices_by_auth_provider_session_id",
            ),
        )

    @trace
    async def get_device_updates_by_remote(
        self, destination: str, from_stream_id: int, limit: int
    ) -> Tuple[int, List[Tuple[str, JsonDict]]]:
        """Get a stream of device updates to send to the given remote server.

        Args:
            destination: The host the device updates are intended for
            from_stream_id: The minimum stream_id to filter updates by, exclusive
            limit: Maximum number of device updates to return

        Returns:
            - The current stream id (i.e. the stream id of the last update included
              in the response); and
            - The list of updates, where each update is a pair of EDU type and
              EDU contents.
        """
        now_stream_id = self.get_device_stream_token()
        if from_stream_id == now_stream_id:
            return now_stream_id, []

        if self._device_list_federation_stream_cache is None:
            raise Exception("Func can only be used on federation senders")

        has_changed = self._device_list_federation_stream_cache.has_entity_changed(
            destination, int(from_stream_id)
        )
        if not has_changed:
            # debugging for https://github.com/matrix-org/synapse/issues/14251
            issue_8631_logger.debug(
                "%s: no change between %i and %i",
                destination,
                from_stream_id,
                now_stream_id,
            )
            return now_stream_id, []

        updates = await self.db_pool.runInteraction(
            "get_device_updates_by_remote",
            self._get_device_updates_by_remote_txn,
            destination,
            from_stream_id,
            now_stream_id,
            limit,
        )

        # We need to ensure `updates` doesn't grow too big.
        # Currently: `len(updates) <= limit`.

        # Return an empty list if there are no updates
        if not updates:
            return now_stream_id, []

        if issue_8631_logger.isEnabledFor(logging.DEBUG):
            data = {(user, device): stream_id for user, device, stream_id, _ in updates}
            issue_8631_logger.debug(
                "device updates need to be sent to %s: %s", destination, data
            )

        # get the cross-signing keys of the users in the list, so that we can
        # determine which of the device changes were cross-signing keys
        users = {r[0] for r in updates}
        master_key_by_user = {}
        self_signing_key_by_user = {}
        for user in users:
            cross_signing_key = await self.get_e2e_cross_signing_key(user, "master")
            if cross_signing_key:
                key_id, verify_key = get_verify_key_from_cross_signing_key(
                    cross_signing_key
                )
                # verify_key is a VerifyKey from signedjson, which uses
                # .version to denote the portion of the key ID after the
                # algorithm and colon, which is the device ID
                master_key_by_user[user] = {
                    "key_info": cross_signing_key,
                    "device_id": verify_key.version,
                }

            cross_signing_key = await self.get_e2e_cross_signing_key(
                user, "self_signing"
            )
            if cross_signing_key:
                key_id, verify_key = get_verify_key_from_cross_signing_key(
                    cross_signing_key
                )
                self_signing_key_by_user[user] = {
                    "key_info": cross_signing_key,
                    "device_id": verify_key.version,
                }

        # Perform the equivalent of a GROUP BY
        #
        # Iterate through the updates list and copy non-duplicate
        # (user_id, device_id) entries into a map, with the value being
        # the max stream_id across each set of duplicate entries
        #
        # maps (user_id, device_id) -> (stream_id, opentracing_context)
        #
        # opentracing_context contains the opentracing metadata for the request
        # that created the poke
        #
        # The most recent request's opentracing_context is used as the
        # context which created the Edu.

        # This is the stream ID that we will return for the consumer to resume
        # following this stream later.
        last_processed_stream_id = from_stream_id

        # A map of (user ID, device ID) to (stream ID, context).
        query_map: Dict[Tuple[str, str], Tuple[int, Optional[str]]] = {}
        cross_signing_keys_by_user: Dict[str, Dict[str, object]] = {}
        for user_id, device_id, update_stream_id, update_context in updates:
            # Calculate the remaining length budget.
            # Note that, for now, each entry in `cross_signing_keys_by_user`
            # gives rise to two device updates in the result, so those cost twice
            # as much (and are the whole reason we need to separately calculate
            # the budget; we know len(updates) <= limit otherwise!)
            # N.B. len() on dicts is cheap since they store their size.
            remaining_length_budget = limit - (
                len(query_map) + 2 * len(cross_signing_keys_by_user)
            )
            assert remaining_length_budget >= 0

            is_master_key_update = (
                user_id in master_key_by_user
                and device_id == master_key_by_user[user_id]["device_id"]
            )
            is_self_signing_key_update = (
                user_id in self_signing_key_by_user
                and device_id == self_signing_key_by_user[user_id]["device_id"]
            )

            is_cross_signing_key_update = (
                is_master_key_update or is_self_signing_key_update
            )

            if (
                is_cross_signing_key_update
                and user_id not in cross_signing_keys_by_user
            ):
                # This will give rise to 2 device updates.
                # If we don't have the budget, stop here!
                if remaining_length_budget < 2:
                    break

            if is_master_key_update:
                result = cross_signing_keys_by_user.setdefault(user_id, {})
                result["master_key"] = master_key_by_user[user_id]["key_info"]
            elif is_self_signing_key_update:
                result = cross_signing_keys_by_user.setdefault(user_id, {})
                result["self_signing_key"] = self_signing_key_by_user[user_id][
                    "key_info"
                ]
            else:
                key = (user_id, device_id)

                if key not in query_map and remaining_length_budget < 1:
                    # We don't have space for a new entry
                    break

                previous_update_stream_id, _ = query_map.get(key, (0, None))

                if update_stream_id > previous_update_stream_id:
                    # FIXME If this overwrites an older update, this discards the
                    #  previous OpenTracing context.
                    #  It might make it harder to track down issues using OpenTracing.
                    #  If there's a good reason why it doesn't matter, a comment here
                    #  about that would not hurt.
                    query_map[key] = (update_stream_id, update_context)

            # As this update has been added to the response, advance the stream
            # position.
            last_processed_stream_id = update_stream_id

        # In the worst case scenario, each update is for a distinct user and is
        # added either to the query_map or to cross_signing_keys_by_user,
        # but not both:
        # len(query_map) + len(cross_signing_keys_by_user) <= len(updates) here,
        # so len(query_map) + len(cross_signing_keys_by_user) <= limit.

        results = await self._get_device_update_edus_by_remote(
            destination, from_stream_id, query_map
        )

        # len(results) <= len(query_map) here,
        # so len(results) + len(cross_signing_keys_by_user) <= limit.

        # Add the updated cross-signing keys to the results list
        for user_id, result in cross_signing_keys_by_user.items():
            result["user_id"] = user_id
            results.append((EduTypes.SIGNING_KEY_UPDATE, result))
            # also send the unstable version
            # FIXME: remove this when enough servers have upgraded
            #        and remove the length budgeting above.
            results.append(("org.matrix.signing_key_update", result))

        if issue_8631_logger.isEnabledFor(logging.DEBUG):
            for user_id, edu in results:
                issue_8631_logger.debug(
                    "device update to %s for %s from %s to %s: %s",
                    destination,
                    user_id,
                    from_stream_id,
                    last_processed_stream_id,
                    edu,
                )

        return last_processed_stream_id, results

    def _get_device_updates_by_remote_txn(
        self,
        txn: LoggingTransaction,
        destination: str,
        from_stream_id: int,
        now_stream_id: int,
        limit: int,
    ) -> List[Tuple[str, str, int, Optional[str]]]:
        """Return device update information for a given remote destination

        Args:
            txn: The transaction to execute
            destination: The host the device updates are intended for
            from_stream_id: The minimum stream_id to filter updates by, exclusive
            now_stream_id: The maximum stream_id to filter updates by, inclusive
            limit: Maximum number of device updates to return

        Returns:
            List of device update tuples:
                - user_id
                - device_id
                - stream_id
                - opentracing_context
        """
        # get the list of device updates that need to be sent
        sql = """
            SELECT user_id, device_id, stream_id, opentracing_context FROM device_lists_outbound_pokes
            WHERE destination = ? AND ? < stream_id AND stream_id <= ?
            ORDER BY stream_id
            LIMIT ?
        """
        txn.execute(sql, (destination, from_stream_id, now_stream_id, limit))

        return cast(List[Tuple[str, str, int, Optional[str]]], txn.fetchall())

    async def _get_device_update_edus_by_remote(
        self,
        destination: str,
        from_stream_id: int,
        query_map: Dict[Tuple[str, str], Tuple[int, Optional[str]]],
    ) -> List[Tuple[str, dict]]:
        """Returns a list of device update EDUs as well as E2EE keys

        Args:
            destination: The host the device updates are intended for
            from_stream_id: The minimum stream_id to filter updates by, exclusive
            query_map: Dictionary mapping (user_id, device_id) to
                (update stream_id, the relevant json-encoded opentracing context)

        Returns:
            List of objects representing a device update EDU.

        Postconditions:
            The returned list has a length not exceeding that of the query_map:
                len(result) <= len(query_map)
        """
        devices = (
            await self.get_e2e_device_keys_and_signatures(
                # Because these are (user_id, device_id) tuples with all
                # device_ids not being None, the returned list's length will not
                # exceed that of query_map.
                query_map.keys(),
                include_all_devices=True,
                include_deleted_devices=True,
            )
            if query_map
            else {}
        )

        results = []
        for user_id, user_devices in devices.items():
            # The prev_id for the first row is always the last row before
            # `from_stream_id`
            prev_id = await self._get_last_device_update_for_remote_user(
                destination, user_id, from_stream_id
            )

            # make sure we go through the devices in stream order
            device_ids = sorted(
                user_devices.keys(),
                key=lambda i: query_map[(user_id, i)][0],
            )

            for device_id in device_ids:
                device = user_devices[device_id]
                stream_id, opentracing_context = query_map[(user_id, device_id)]
                result = {
                    "user_id": user_id,
                    "device_id": device_id,
                    "prev_id": [prev_id] if prev_id else [],
                    "stream_id": stream_id,
                }

                if opentracing_context != "{}":
                    result["org.matrix.opentracing_context"] = opentracing_context

                prev_id = stream_id

                if device is not None:
                    keys = device.keys
                    if keys:
                        result["keys"] = keys

                    device_display_name = None
                    if self.hs.config.federation.allow_device_name_lookup_over_federation:
                        device_display_name = device.display_name
                    if device_display_name:
                        result["device_display_name"] = device_display_name
                else:
                    result["deleted"] = True

                results.append((EduTypes.DEVICE_LIST_UPDATE, result))

        return results

    async def _get_last_device_update_for_remote_user(
        self, destination: str, user_id: str, from_stream_id: int
    ) -> int:
        def f(txn: LoggingTransaction) -> int:
            prev_sent_id_sql = """
                SELECT coalesce(max(stream_id), 0) as stream_id
                FROM device_lists_outbound_last_success
                WHERE destination = ? AND user_id = ? AND stream_id <= ?
            """
            txn.execute(prev_sent_id_sql, (destination, user_id, from_stream_id))
            rows = txn.fetchall()
            return rows[0][0]

        return await self.db_pool.runInteraction(
            "get_last_device_update_for_remote_user", f
        )

    async def mark_as_sent_devices_by_remote(
        self, destination: str, stream_id: int
    ) -> None:
        """Mark that updates have successfully been sent to the destination."""
        await self.db_pool.runInteraction(
            "mark_as_sent_devices_by_remote",
            self._mark_as_sent_devices_by_remote_txn,
            destination,
            stream_id,
        )

    def _mark_as_sent_devices_by_remote_txn(
        self, txn: LoggingTransaction, destination: str, stream_id: int
    ) -> None:
        # We update the device_lists_outbound_last_success with the successfully
        # poked users.
        sql = """
            SELECT user_id, coalesce(max(o.stream_id), 0)
            FROM device_lists_outbound_pokes as o
            WHERE destination = ? AND o.stream_id <= ?
            GROUP BY user_id
        """
        txn.execute(sql, (destination, stream_id))
        rows = txn.fetchall()

        self.db_pool.simple_upsert_many_txn(
            txn=txn,
            table="device_lists_outbound_last_success",
            key_names=("destination", "user_id"),
            key_values=[(destination, user_id) for user_id, _ in rows],
            value_names=("stream_id",),
            value_values=[(stream_id,) for _, stream_id in rows],
        )

        # Delete all sent outbound pokes
        sql = """
            DELETE FROM device_lists_outbound_pokes
            WHERE destination = ? AND stream_id <= ?
        """
        txn.execute(sql, (destination, stream_id))

    async def add_user_signature_change_to_streams(
        self, from_user_id: str, user_ids: List[str]
    ) -> int:
        """Persist that a user has made new signatures

        Args:
            from_user_id: the user who made the signatures
            user_ids: the users who were signed

        Returns:
            The new stream ID.
        """

        async with self._device_list_id_gen.get_next() as stream_id:
            await self.db_pool.runInteraction(
                "add_user_sig_change_to_streams",
                self._add_user_signature_change_txn,
                from_user_id,
                user_ids,
                stream_id,
            )
        return stream_id

    def _add_user_signature_change_txn(
        self,
        txn: LoggingTransaction,
        from_user_id: str,
        user_ids: List[str],
        stream_id: int,
    ) -> None:
        txn.call_after(
            self._user_signature_stream_cache.entity_has_changed,
            from_user_id,
            stream_id,
        )
        self.db_pool.simple_insert_txn(
            txn,
            "user_signature_stream",
            values={
                "stream_id": stream_id,
                "from_user_id": from_user_id,
                "user_ids": json_encoder.encode(user_ids),
                "instance_name": self._instance_name,
            },
        )

    @trace
    @cancellable
    async def get_user_devices_from_cache(
        self, user_ids: Set[str], user_and_device_ids: List[Tuple[str, str]]
    ) -> Tuple[Set[str], Dict[str, Mapping[str, JsonMapping]]]:
        """Get the devices (and keys if any) for remote users from the cache.

        Args:
            user_ids: users which should have all device IDs returned
            user_and_device_ids: List of (user_id, device_ids)

        Returns:
            A tuple of (user_ids_not_in_cache, results_map), where
            user_ids_not_in_cache is a set of user_ids and results_map is a
            mapping of user_id -> device_id -> device_info.
        """
        unique_user_ids = user_ids | {user_id for user_id, _ in user_and_device_ids}

        user_ids_in_cache = await self.get_users_whose_devices_are_cached(
            unique_user_ids
        )
        user_ids_not_in_cache = unique_user_ids - user_ids_in_cache

        # First fetch all the users which all devices are to be returned.
        results: Dict[str, Mapping[str, JsonMapping]] = {}
        for user_id in user_ids:
            if user_id in user_ids_in_cache:
                results[user_id] = await self.get_cached_devices_for_user(user_id)
        # Then fetch all device-specific requests, but skip users we've already
        # fetched all devices for.
        device_specific_results: Dict[str, Dict[str, JsonMapping]] = {}
        for user_id, device_id in user_and_device_ids:
            if user_id in user_ids_in_cache and user_id not in user_ids:
                device = await self._get_cached_user_device(user_id, device_id)
                device_specific_results.setdefault(user_id, {})[device_id] = device
        results.update(device_specific_results)

        set_tag("in_cache", str(results))
        set_tag("not_in_cache", str(user_ids_not_in_cache))

        return user_ids_not_in_cache, results

    async def get_users_whose_devices_are_cached(
        self, user_ids: StrCollection
    ) -> Set[str]:
        """Checks which of the given users we have cached the devices for."""
        user_map = await self.get_device_list_last_stream_id_for_remotes(user_ids)

        # We go and check if any of the users need to have their device lists
        # resynced. If they do then we remove them from the cached list.
        users_needing_resync = await self.get_user_ids_requiring_device_list_resync(
            user_ids
        )
        user_ids_in_cache = {
            user_id for user_id, stream_id in user_map.items() if stream_id
        } - users_needing_resync
        return user_ids_in_cache

    @cached(num_args=2, tree=True)
    async def _get_cached_user_device(
        self, user_id: str, device_id: str
    ) -> JsonMapping:
        content = await self.db_pool.simple_select_one_onecol(
            table="device_lists_remote_cache",
            keyvalues={"user_id": user_id, "device_id": device_id},
            retcol="content",
            desc="_get_cached_user_device",
        )
        return db_to_json(content)

    @cached()
    async def get_cached_devices_for_user(
        self, user_id: str
    ) -> Mapping[str, JsonMapping]:
        devices = cast(
            List[Tuple[str, str]],
            await self.db_pool.simple_select_list(
                table="device_lists_remote_cache",
                keyvalues={"user_id": user_id},
                retcols=("device_id", "content"),
                desc="get_cached_devices_for_user",
            ),
        )
        return {device[0]: db_to_json(device[1]) for device in devices}

    @cancellable
    async def get_all_devices_changed(
        self,
        from_key: int,
        to_key: int,
    ) -> Set[str]:
        """Get all users whose devices have changed in the given range.

        Args:
            from_key: The minimum device lists stream token to query device list
                changes for, exclusive.
            to_key: The maximum device lists stream token to query device list
                changes for, inclusive.

        Returns:
            The set of user_ids whose devices have changed since `from_key`
            (exclusive) until `to_key` (inclusive).
        """

        result = self._device_list_stream_cache.get_all_entities_changed(from_key)

        if result.hit:
            # We know which users might have changed devices.
            if not result.entities:
                # If no users then we can return early.
                return set()

            # Otherwise we need to filter down the list
            return await self.get_users_whose_devices_changed(
                from_key, result.entities, to_key
            )

        # If the cache didn't tell us anything, we just need to query the full
        # range.
        sql = """
            SELECT DISTINCT user_id FROM device_lists_stream
            WHERE ? < stream_id AND stream_id <= ?
        """

        rows = await self.db_pool.execute(
            "get_all_devices_changed",
            sql,
            from_key,
            to_key,
        )
        return {u for (u,) in rows}

    @cancellable
    async def get_users_whose_devices_changed(
        self,
        from_key: int,
        user_ids: Collection[str],
        to_key: Optional[int] = None,
    ) -> Set[str]:
        """Get set of users whose devices have changed since `from_key` that
        are in the given list of user_ids.

        Args:
            from_key: The minimum device lists stream token to query device list changes for,
                exclusive.
            user_ids: If provided, only check if these users have changed their device lists.
                Otherwise changes from all users are returned.
            to_key: The maximum device lists stream token to query device list changes for,
                inclusive.

        Returns:
            The set of user_ids whose devices have changed since `from_key` (exclusive)
                until `to_key` (inclusive).
        """
        # Get set of users who *may* have changed. Users not in the returned
        # list have definitely not changed.
        user_ids_to_check = self._device_list_stream_cache.get_entities_changed(
            user_ids, from_key
        )

        # If an empty set was returned, there's nothing to do.
        if not user_ids_to_check:
            return set()

        if to_key is None:
            to_key = self._device_list_id_gen.get_current_token()

        def _get_users_whose_devices_changed_txn(txn: LoggingTransaction) -> Set[str]:
            sql = """
                SELECT DISTINCT user_id FROM device_lists_stream
                WHERE  ? < stream_id AND stream_id <= ? AND %s
            """

            changes: Set[str] = set()

            # Query device changes with a batch of users at a time
            for chunk in batch_iter(user_ids_to_check, 100):
                clause, args = make_in_list_sql_clause(
                    txn.database_engine, "user_id", chunk
                )
                txn.execute(sql % (clause,), [from_key, to_key] + args)
                changes.update(user_id for (user_id,) in txn)

            return changes

        return await self.db_pool.runInteraction(
            "get_users_whose_devices_changed", _get_users_whose_devices_changed_txn
        )

    async def get_users_whose_signatures_changed(
        self, user_id: str, from_key: int
    ) -> Set[str]:
        """Get the users who have new cross-signing signatures made by `user_id` since
        `from_key`.

        Args:
            user_id: the user who made the signatures
            from_key: The device lists stream token

        Returns:
            A set of user IDs with updated signatures.
        """

        if self._user_signature_stream_cache.has_entity_changed(user_id, from_key):
            sql = """
                SELECT DISTINCT user_ids FROM user_signature_stream
                WHERE from_user_id = ? AND stream_id > ?
            """
            rows = await self.db_pool.execute(
                "get_users_whose_signatures_changed", sql, user_id, from_key
            )
            return {user for row in rows for user in db_to_json(row[0])}
        else:
            return set()

    async def get_all_device_list_changes_for_remotes(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, tuple]], int, bool]:
        """Get updates for device lists replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updates.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        def _get_all_device_list_changes_for_remotes(
            txn: Cursor,
        ) -> Tuple[List[Tuple[int, tuple]], int, bool]:
            # This query Does The Right Thing where it'll correctly apply the
            # bounds to the inner queries.
            sql = """
                SELECT stream_id, user_id, hosts FROM (
                    SELECT stream_id, user_id, false AS hosts FROM device_lists_stream
                    UNION ALL
                    SELECT DISTINCT stream_id, user_id, true AS hosts FROM device_lists_outbound_pokes
                ) AS e
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC
                LIMIT ?
            """

            txn.execute(sql, (last_id, current_id, limit))
            updates = [(row[0], row[1:]) for row in txn]
            limited = False
            upto_token = current_id
            if len(updates) >= limit:
                upto_token = updates[-1][0]
                limited = True

            return updates, upto_token, limited

        return await self.db_pool.runInteraction(
            "get_all_device_list_changes_for_remotes",
            _get_all_device_list_changes_for_remotes,
        )

    @cached(max_entries=10000)
    async def get_device_list_last_stream_id_for_remote(
        self, user_id: str
    ) -> Optional[str]:
        """Get the last stream_id we got for a user. May be None if we haven't
        got any information for them.
        """
        return await self.db_pool.simple_select_one_onecol(
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            retcol="stream_id",
            desc="get_device_list_last_stream_id_for_remote",
            allow_none=True,
        )

    @cachedList(
        cached_method_name="get_device_list_last_stream_id_for_remote",
        list_name="user_ids",
    )
    async def get_device_list_last_stream_id_for_remotes(
        self, user_ids: Iterable[str]
    ) -> Mapping[str, Optional[str]]:
        rows = cast(
            List[Tuple[str, str]],
            await self.db_pool.simple_select_many_batch(
                table="device_lists_remote_extremeties",
                column="user_id",
                iterable=user_ids,
                retcols=("user_id", "stream_id"),
                desc="get_device_list_last_stream_id_for_remotes",
            ),
        )

        results: Dict[str, Optional[str]] = {user_id: None for user_id in user_ids}
        results.update(rows)

        return results

    async def get_user_ids_requiring_device_list_resync(
        self,
        user_ids: Optional[Collection[str]] = None,
    ) -> Set[str]:
        """Given a list of remote users return the list of users that we
        should resync the device lists for. If None is given instead of a list,
        return every user that we should resync the device lists for.

        Returns:
            The IDs of users whose device lists need resync.
        """
        if user_ids:
            rows = cast(
                List[Tuple[str]],
                await self.db_pool.simple_select_many_batch(
                    table="device_lists_remote_resync",
                    column="user_id",
                    iterable=user_ids,
                    retcols=("user_id",),
                    desc="get_user_ids_requiring_device_list_resync_with_iterable",
                ),
            )
        else:
            rows = cast(
                List[Tuple[str]],
                await self.db_pool.simple_select_list(
                    table="device_lists_remote_resync",
                    keyvalues=None,
                    retcols=("user_id",),
                    desc="get_user_ids_requiring_device_list_resync",
                ),
            )

        return {row[0] for row in rows}

    async def mark_remote_users_device_caches_as_stale(
        self, user_ids: StrCollection
    ) -> None:
        """Records that the server has reason to believe the cache of the devices
        for the remote users is out of date.
        """

        def _mark_remote_users_device_caches_as_stale_txn(
            txn: LoggingTransaction,
        ) -> None:
            # TODO add insertion_values support to simple_upsert_many and use
            #      that!
            for user_id in user_ids:
                self.db_pool.simple_upsert_txn(
                    txn,
                    table="device_lists_remote_resync",
                    keyvalues={"user_id": user_id},
                    values={},
                    insertion_values={"added_ts": self._clock.time_msec()},
                )

        await self.db_pool.runInteraction(
            "mark_remote_users_device_caches_as_stale",
            _mark_remote_users_device_caches_as_stale_txn,
        )

    async def mark_remote_user_device_cache_as_valid(self, user_id: str) -> None:
        # Remove the database entry that says we need to resync devices, after a resync
        await self.db_pool.simple_delete(
            table="device_lists_remote_resync",
            keyvalues={"user_id": user_id},
            desc="mark_remote_user_device_cache_as_valid",
        )

    async def handle_potentially_left_users(self, user_ids: Set[str]) -> None:
        """Given a set of remote users check if the server still shares a room with
        them. If not then mark those users' device cache as stale.
        """

        if not user_ids:
            return

        await self.db_pool.runInteraction(
            "_handle_potentially_left_users",
            self.handle_potentially_left_users_txn,
            user_ids,
        )

    def handle_potentially_left_users_txn(
        self,
        txn: LoggingTransaction,
        user_ids: Set[str],
    ) -> None:
        """Given a set of remote users check if the server still shares a room with
        them. If not then mark those users' device cache as stale.
        """

        if not user_ids:
            return

        joined_users = self.get_users_server_still_shares_room_with_txn(txn, user_ids)
        left_users = user_ids - joined_users

        for user_id in left_users:
            self.mark_remote_user_device_list_as_unsubscribed_txn(txn, user_id)

    async def mark_remote_user_device_list_as_unsubscribed(self, user_id: str) -> None:
        """Mark that we no longer track device lists for remote user."""

        await self.db_pool.runInteraction(
            "mark_remote_user_device_list_as_unsubscribed",
            self.mark_remote_user_device_list_as_unsubscribed_txn,
            user_id,
        )

    def mark_remote_user_device_list_as_unsubscribed_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
    ) -> None:
        self.db_pool.simple_delete_txn(
            txn,
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
        )
        self._invalidate_cache_and_stream(
            txn, self.get_device_list_last_stream_id_for_remote, (user_id,)
        )

    async def get_dehydrated_device(
        self, user_id: str
    ) -> Optional[Tuple[str, JsonDict]]:
        """Retrieve the information for a dehydrated device.

        Args:
            user_id: the user whose dehydrated device we are looking for
        Returns:
            a tuple whose first item is the device ID, and the second item is
            the dehydrated device information
        """
        # FIXME: make sure device ID still exists in devices table
        row = await self.db_pool.simple_select_one(
            table="dehydrated_devices",
            keyvalues={"user_id": user_id},
            retcols=["device_id", "device_data"],
            allow_none=True,
        )
        return (row[0], json_decoder.decode(row[1])) if row else None

    def _store_dehydrated_device_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_id: str,
        device_data: str,
        time: int,
        keys: Optional[JsonDict] = None,
    ) -> Optional[str]:
        # TODO: make keys non-optional once support for msc2697 is dropped
        if keys:
            device_keys = keys.get("device_keys", None)
            if device_keys:
                # Type ignore - this function is defined on EndToEndKeyStore which we do
                # have access to due to hs.get_datastore() "magic"
                self._set_e2e_device_keys_txn(  # type: ignore[attr-defined]
                    txn, user_id, device_id, time, device_keys
                )

            one_time_keys = keys.get("one_time_keys", None)
            if one_time_keys:
                key_list = []
                for key_id, key_obj in one_time_keys.items():
                    algorithm, key_id = key_id.split(":")
                    key_list.append(
                        (
                            algorithm,
                            key_id,
                            encode_canonical_json(key_obj).decode("ascii"),
                        )
                    )
                self._add_e2e_one_time_keys_txn(txn, user_id, device_id, time, key_list)

            fallback_keys = keys.get("fallback_keys", None)
            if fallback_keys:
                self._set_e2e_fallback_keys_txn(txn, user_id, device_id, fallback_keys)

        old_device_id = self.db_pool.simple_select_one_onecol_txn(
            txn,
            table="dehydrated_devices",
            keyvalues={"user_id": user_id},
            retcol="device_id",
            allow_none=True,
        )
        self.db_pool.simple_upsert_txn(
            txn,
            table="dehydrated_devices",
            keyvalues={"user_id": user_id},
            values={"device_id": device_id, "device_data": device_data},
        )

        return old_device_id

    async def store_dehydrated_device(
        self,
        user_id: str,
        device_id: str,
        device_data: JsonDict,
        time_now: int,
        keys: Optional[dict] = None,
    ) -> Optional[str]:
        """Store a dehydrated device for a user.

        Args:
            user_id: the user that we are storing the device for
            device_id: the ID of the dehydrated device
            device_data: the dehydrated device information
            time_now: current time at the request in milliseconds
            keys: keys for the dehydrated device

        Returns:
            device id of the user's previous dehydrated device, if any
        """

        return await self.db_pool.runInteraction(
            "store_dehydrated_device_txn",
            self._store_dehydrated_device_txn,
            user_id,
            device_id,
            json_encoder.encode(device_data),
            time_now,
            keys,
        )

    async def remove_dehydrated_device(self, user_id: str, device_id: str) -> bool:
        """Remove a dehydrated device.

        Args:
            user_id: the user that the dehydrated device belongs to
            device_id: the ID of the dehydrated device
        """
        count = await self.db_pool.simple_delete(
            "dehydrated_devices",
            {"user_id": user_id, "device_id": device_id},
            desc="remove_dehydrated_device",
        )
        return count >= 1

    @wrap_as_background_process("prune_old_outbound_device_pokes")
    async def _prune_old_outbound_device_pokes(
        self, prune_age: int = 24 * 60 * 60 * 1000
    ) -> None:
        """Delete old entries out of the device_lists_outbound_pokes to ensure
        that we don't fill up due to dead servers.

        Normally, we try to send device updates as a delta since a previous known point:
        this is done by setting the prev_id in the m.device_list_update EDU. However,
        for that to work, we have to have a complete record of each change to
        each device, which can add up to quite a lot of data.

        An alternative mechanism is that, if the remote server sees that it has missed
        an entry in the stream_id sequence for a given user, it will request a full
        list of that user's devices. Hence, we can reduce the amount of data we have to
        store (and transmit in some future transaction), by clearing almost everything
        for a given destination out of the database, and having the remote server
        resync.

        All we need to do is make sure we keep at least one row for each
        (user, destination) pair, to remind us to send a m.device_list_update EDU for
        that user when the destination comes back. It doesn't matter which device
        we keep.
        """
        yesterday = self._clock.time_msec() - prune_age

        def _prune_txn(txn: LoggingTransaction) -> None:
            # look for (user, destination) pairs which have an update older than
            # the cutoff.
            #
            # For each pair, we also need to know the most recent stream_id, and
            # an arbitrary device_id at that stream_id.
            select_sql = """
            SELECT
                dlop1.destination,
                dlop1.user_id,
                MAX(dlop1.stream_id) AS stream_id,
                (SELECT MIN(dlop2.device_id) AS device_id FROM
                    device_lists_outbound_pokes dlop2
                    WHERE dlop2.destination = dlop1.destination AND
                      dlop2.user_id=dlop1.user_id AND
                      dlop2.stream_id=MAX(dlop1.stream_id)
                )
            FROM device_lists_outbound_pokes dlop1
                GROUP BY destination, user_id
                HAVING min(ts) < ? AND count(*) > 1
            """

            txn.execute(select_sql, (yesterday,))
            rows = txn.fetchall()

            if not rows:
                return

            logger.info(
                "Pruning old outbound device list updates for %i users/destinations: %s",
                len(rows),
                shortstr((row[0], row[1]) for row in rows),
            )

            # we want to keep the update with the highest stream_id for each user.
            #
            # there might be more than one update (with different device_ids) with the
            # same stream_id, so we also delete all but one rows with the max stream id.
            delete_sql = """
                DELETE FROM device_lists_outbound_pokes
                WHERE destination = ? AND user_id = ? AND (
                    stream_id < ? OR
                    (stream_id = ? AND device_id != ?)
                )
            """
            count = 0
            for destination, user_id, stream_id, device_id in rows:
                txn.execute(
                    delete_sql, (destination, user_id, stream_id, stream_id, device_id)
                )
                count += txn.rowcount

            # Since we've deleted unsent deltas, we need to remove the entry
            # of last successful sent so that the prev_ids are correctly set.
            sql = """
                DELETE FROM device_lists_outbound_last_success
                WHERE destination = ? AND user_id = ?
            """
            txn.execute_batch(sql, [(row[0], row[1]) for row in rows])

            logger.info("Pruned %d device list outbound pokes", count)

        await self.db_pool.runInteraction(
            "_prune_old_outbound_device_pokes",
            _prune_txn,
        )

    async def get_local_devices_not_accessed_since(
        self, since_ms: int
    ) -> Dict[str, List[str]]:
        """Retrieves local devices that haven't been accessed since a given date.

        Args:
            since_ms: the timestamp to select on, every device with a last access date
                from before that time is returned.

        Returns:
            A dictionary with an entry for each user with at least one device matching
            the request, which value is a list of the device ID(s) for the corresponding
            device(s).
        """

        def get_devices_not_accessed_since_txn(
            txn: LoggingTransaction,
        ) -> List[Tuple[str, str]]:
            sql = """
                SELECT user_id, device_id
                FROM devices WHERE last_seen < ? AND hidden = FALSE
            """
            txn.execute(sql, (since_ms,))
            return cast(List[Tuple[str, str]], txn.fetchall())

        rows = await self.db_pool.runInteraction(
            "get_devices_not_accessed_since",
            get_devices_not_accessed_since_txn,
        )

        devices: Dict[str, List[str]] = {}
        for user_id, device_id in rows:
            # Remote devices are never stale from our point of view.
            if self.hs.is_mine_id(user_id):
                user_devices = devices.setdefault(user_id, [])
                user_devices.append(device_id)

        return devices

    @cached()
    async def _get_min_device_lists_changes_in_room(self) -> int:
        """Returns the minimum stream ID that we have entries for
        `device_lists_changes_in_room`
        """

        return await self.db_pool.simple_select_one_onecol(
            table="device_lists_changes_in_room",
            keyvalues={},
            retcol="COALESCE(MIN(stream_id), 0)",
            desc="get_min_device_lists_changes_in_room",
        )

    @cancellable
    async def get_device_list_changes_in_rooms(
        self, room_ids: Collection[str], from_id: int, to_id: int
    ) -> Optional[Set[str]]:
        """Return the set of users whose devices have changed in the given rooms
        since the given stream ID.

        Returns None if the given stream ID is too old.
        """

        if not room_ids:
            return set()

        min_stream_id = await self._get_min_device_lists_changes_in_room()

        if min_stream_id > from_id:
            return None

        changed_room_ids = self._device_list_room_stream_cache.get_entities_changed(
            room_ids, from_id
        )
        if not changed_room_ids:
            return set()

        sql = """
            SELECT DISTINCT user_id FROM device_lists_changes_in_room
            WHERE {clause} AND stream_id > ? AND stream_id <= ?
        """

        def _get_device_list_changes_in_rooms_txn(
            txn: LoggingTransaction,
            clause: str,
            args: List[Any],
        ) -> Set[str]:
            txn.execute(sql.format(clause=clause), args)
            return {user_id for (user_id,) in txn}

        changes = set()
        for chunk in batch_iter(changed_room_ids, 1000):
            clause, args = make_in_list_sql_clause(
                self.database_engine, "room_id", chunk
            )
            args.append(from_id)
            args.append(to_id)

            changes |= await self.db_pool.runInteraction(
                "get_device_list_changes_in_rooms",
                _get_device_list_changes_in_rooms_txn,
                clause,
                args,
            )

        return changes

    async def get_all_device_list_changes(self, from_id: int, to_id: int) -> Set[str]:
        """Return the set of rooms where devices have changed since the given
        stream ID.

        Will raise an exception if the given stream ID is too old.
        """

        min_stream_id = await self._get_min_device_lists_changes_in_room()

        if min_stream_id > from_id:
            raise Exception("stream ID is too old")

        sql = """
            SELECT DISTINCT room_id FROM device_lists_changes_in_room
            WHERE stream_id > ? AND stream_id <= ?
        """

        def _get_all_device_list_changes_txn(
            txn: LoggingTransaction,
        ) -> Set[str]:
            txn.execute(sql, (from_id, to_id))
            return {room_id for (room_id,) in txn}

        return await self.db_pool.runInteraction(
            "get_all_device_list_changes",
            _get_all_device_list_changes_txn,
        )

    async def get_device_list_changes_in_room(
        self, room_id: str, min_stream_id: int
    ) -> Collection[Tuple[str, str]]:
        """Get all device list changes that happened in the room since the given
        stream ID.

        Returns:
            Collection of user ID/device ID tuples of all devices that have
            changed
        """

        sql = """
            SELECT DISTINCT user_id, device_id FROM device_lists_changes_in_room
            WHERE room_id = ? AND stream_id > ?
        """

        def get_device_list_changes_in_room_txn(
            txn: LoggingTransaction,
        ) -> Collection[Tuple[str, str]]:
            txn.execute(sql, (room_id, min_stream_id))
            return cast(Collection[Tuple[str, str]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_device_list_changes_in_room",
            get_device_list_changes_in_room_txn,
        )

    async def get_destinations_for_device(self, stream_id: int) -> StrCollection:
        return await self.db_pool.simple_select_onecol(
            table="device_lists_outbound_pokes",
            keyvalues={"stream_id": stream_id},
            retcol="destination",
            desc="get_destinations_for_device",
        )


class DeviceBackgroundUpdateStore(SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self._instance_name = hs.get_instance_name()

        self.db_pool.updates.register_background_index_update(
            "device_lists_stream_idx",
            index_name="device_lists_stream_user_id",
            table="device_lists_stream",
            columns=["user_id", "device_id"],
        )

        # create a unique index on device_lists_remote_cache
        self.db_pool.updates.register_background_index_update(
            "device_lists_remote_cache_unique_idx",
            index_name="device_lists_remote_cache_unique_id",
            table="device_lists_remote_cache",
            columns=["user_id", "device_id"],
            unique=True,
        )

        # And one on device_lists_remote_extremeties
        self.db_pool.updates.register_background_index_update(
            "device_lists_remote_extremeties_unique_idx",
            index_name="device_lists_remote_extremeties_unique_idx",
            table="device_lists_remote_extremeties",
            columns=["user_id"],
            unique=True,
        )

        # once they complete, we can remove the old non-unique indexes.
        self.db_pool.updates.register_background_update_handler(
            DROP_DEVICE_LIST_STREAMS_NON_UNIQUE_INDEXES,
            self._drop_device_list_streams_non_unique_indexes,
        )

        # clear out duplicate device list outbound pokes
        self.db_pool.updates.register_background_update_handler(
            BG_UPDATE_REMOVE_DUP_OUTBOUND_POKES,
            self._remove_duplicate_outbound_pokes,
        )

        self.db_pool.updates.register_background_index_update(
            "device_lists_changes_in_room_by_room_index",
            index_name="device_lists_changes_in_room_by_room_idx",
            table="device_lists_changes_in_room",
            columns=["room_id", "stream_id"],
        )

    async def _drop_device_list_streams_non_unique_indexes(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        def f(conn: LoggingDatabaseConnection) -> None:
            txn = conn.cursor()
            txn.execute("DROP INDEX IF EXISTS device_lists_remote_cache_id")
            txn.execute("DROP INDEX IF EXISTS device_lists_remote_extremeties_id")
            txn.close()

        await self.db_pool.runWithConnection(f)
        await self.db_pool.updates._end_background_update(
            DROP_DEVICE_LIST_STREAMS_NON_UNIQUE_INDEXES
        )
        return 1

    async def _remove_duplicate_outbound_pokes(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        # for some reason, we have accumulated duplicate entries in
        # device_lists_outbound_pokes, which makes prune_outbound_device_list_pokes less
        # efficient.
        #
        # For each duplicate, we delete all the existing rows and put one back.

        last_row = progress.get(
            "last_row",
            {"stream_id": 0, "destination": "", "user_id": "", "device_id": ""},
        )

        def _txn(txn: LoggingTransaction) -> int:
            clause, args = make_tuple_comparison_clause(
                [
                    ("stream_id", last_row["stream_id"]),
                    ("destination", last_row["destination"]),
                    ("user_id", last_row["user_id"]),
                    ("device_id", last_row["device_id"]),
                ]
            )
            sql = f"""
                SELECT stream_id, destination, user_id, device_id, MAX(ts) AS ts
                FROM device_lists_outbound_pokes
                WHERE {clause}
                GROUP BY stream_id, destination, user_id, device_id
                HAVING count(*) > 1
                ORDER BY stream_id, destination, user_id, device_id
                LIMIT ?
                """
            txn.execute(sql, args + [batch_size])
            rows = txn.fetchall()

            stream_id, destination, user_id, device_id = None, None, None, None
            for stream_id, destination, user_id, device_id, _ in rows:
                self.db_pool.simple_delete_txn(
                    txn,
                    "device_lists_outbound_pokes",
                    {
                        "stream_id": stream_id,
                        "destination": destination,
                        "user_id": user_id,
                        "device_id": device_id,
                    },
                )

                self.db_pool.simple_insert_txn(
                    txn,
                    "device_lists_outbound_pokes",
                    {
                        "stream_id": stream_id,
                        "instance_name": self._instance_name,
                        "destination": destination,
                        "user_id": user_id,
                        "device_id": device_id,
                        "sent": False,
                    },
                )

            if rows:
                self.db_pool.updates._background_update_progress_txn(
                    txn,
                    BG_UPDATE_REMOVE_DUP_OUTBOUND_POKES,
                    {
                        "last_row": {
                            "stream_id": stream_id,
                            "destination": destination,
                            "user_id": user_id,
                            "device_id": device_id,
                        }
                    },
                )

            return len(rows)

        rows = await self.db_pool.runInteraction(
            BG_UPDATE_REMOVE_DUP_OUTBOUND_POKES, _txn
        )

        if not rows:
            await self.db_pool.updates._end_background_update(
                BG_UPDATE_REMOVE_DUP_OUTBOUND_POKES
            )

        return rows


class DeviceStore(DeviceWorkerStore, DeviceBackgroundUpdateStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        # Map of (user_id, device_id) -> bool. If there is an entry that implies
        # the device exists.
        self.device_id_exists_cache: LruCache[Tuple[str, str], Literal[True]] = (
            LruCache(cache_name="device_id_exists", max_size=10000)
        )

    async def store_device(
        self,
        user_id: str,
        device_id: str,
        initial_device_display_name: Optional[str],
        auth_provider_id: Optional[str] = None,
        auth_provider_session_id: Optional[str] = None,
    ) -> bool:
        """Ensure the given device is known; add it to the store if not

        Args:
            user_id: id of user associated with the device
            device_id: id of device
            initial_device_display_name: initial displayname of the device.
                Ignored if device exists.
            auth_provider_id: The SSO IdP the user used, if any.
            auth_provider_session_id: The session ID (sid) got from a OIDC login.

        Returns:
            Whether the device was inserted or an existing device existed with that ID.

        Raises:
            StoreError: if the device is already in use
        """
        key = (user_id, device_id)
        if self.device_id_exists_cache.get(key, None):
            return False

        try:
            inserted = await self.db_pool.simple_upsert(
                "devices",
                keyvalues={
                    "user_id": user_id,
                    "device_id": device_id,
                },
                values={},
                insertion_values={
                    "display_name": initial_device_display_name,
                    "hidden": False,
                },
                desc="store_device",
            )
            if not inserted:
                # if the device already exists, check if it's a real device, or
                # if the device ID is reserved by something else
                hidden = await self.db_pool.simple_select_one_onecol(
                    "devices",
                    keyvalues={"user_id": user_id, "device_id": device_id},
                    retcol="hidden",
                )
                if hidden:
                    raise StoreError(400, "The device ID is in use", Codes.FORBIDDEN)

            if auth_provider_id and auth_provider_session_id:
                await self.db_pool.simple_insert(
                    "device_auth_providers",
                    values={
                        "user_id": user_id,
                        "device_id": device_id,
                        "auth_provider_id": auth_provider_id,
                        "auth_provider_session_id": auth_provider_session_id,
                    },
                    desc="store_device_auth_provider",
                )

            self.device_id_exists_cache.set(key, True)
            return inserted
        except StoreError:
            raise
        except Exception as e:
            logger.error(
                "store_device with device_id=%s(%r) user_id=%s(%r)"
                " display_name=%s(%r) failed: %s",
                type(device_id).__name__,
                device_id,
                type(user_id).__name__,
                user_id,
                type(initial_device_display_name).__name__,
                initial_device_display_name,
                e,
            )
            raise StoreError(500, "Problem storing device.")

    async def delete_devices(self, user_id: str, device_ids: List[str]) -> None:
        """Deletes several devices.

        Args:
            user_id: The ID of the user which owns the devices
            device_ids: The IDs of the devices to delete
        """

        def _delete_devices_txn(txn: LoggingTransaction, device_ids: List[str]) -> None:
            self.db_pool.simple_delete_many_txn(
                txn,
                table="devices",
                column="device_id",
                values=device_ids,
                keyvalues={"user_id": user_id, "hidden": False},
            )

            self.db_pool.simple_delete_many_txn(
                txn,
                table="device_auth_providers",
                column="device_id",
                values=device_ids,
                keyvalues={"user_id": user_id},
            )

        for batch in batch_iter(device_ids, 100):
            await self.db_pool.runInteraction(
                "delete_devices", _delete_devices_txn, batch
            )

        for device_id in device_ids:
            self.device_id_exists_cache.invalidate((user_id, device_id))

    async def update_device(
        self, user_id: str, device_id: str, new_display_name: Optional[str] = None
    ) -> None:
        """Update a device. Only updates the device if it is not marked as
        hidden.

        Args:
            user_id: The ID of the user which owns the device
            device_id: The ID of the device to update
            new_display_name: new displayname for device; None to leave unchanged
        Raises:
            StoreError: if the device is not found
        """
        updates = {}
        if new_display_name is not None:
            updates["display_name"] = new_display_name
        if not updates:
            return None
        await self.db_pool.simple_update_one(
            table="devices",
            keyvalues={"user_id": user_id, "device_id": device_id, "hidden": False},
            updatevalues=updates,
            desc="update_device",
        )

    async def update_remote_device_list_cache_entry(
        self, user_id: str, device_id: str, content: JsonDict, stream_id: str
    ) -> None:
        """Updates a single device in the cache of a remote user's devicelist.

        Note: assumes that we are the only thread that can be updating this user's
        device list.

        Args:
            user_id: User to update device list for
            device_id: ID of decivice being updated
            content: new data on this device
            stream_id: the version of the device list
        """
        await self.db_pool.runInteraction(
            "update_remote_device_list_cache_entry",
            self._update_remote_device_list_cache_entry_txn,
            user_id,
            device_id,
            content,
            stream_id,
        )

    def _update_remote_device_list_cache_entry_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_id: str,
        content: JsonDict,
        stream_id: str,
    ) -> None:
        """Delete, update or insert a cache entry for this (user, device) pair."""
        if content.get("deleted"):
            self.db_pool.simple_delete_txn(
                txn,
                table="device_lists_remote_cache",
                keyvalues={"user_id": user_id, "device_id": device_id},
            )

            txn.call_after(self.device_id_exists_cache.invalidate, (user_id, device_id))
        else:
            self.db_pool.simple_upsert_txn(
                txn,
                table="device_lists_remote_cache",
                keyvalues={"user_id": user_id, "device_id": device_id},
                values={"content": json_encoder.encode(content)},
            )

        txn.call_after(self._get_cached_user_device.invalidate, (user_id, device_id))
        txn.call_after(self.get_cached_devices_for_user.invalidate, (user_id,))
        txn.call_after(
            self.get_device_list_last_stream_id_for_remote.invalidate, (user_id,)
        )

        self.db_pool.simple_upsert_txn(
            txn,
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            values={"stream_id": stream_id},
        )

    async def update_remote_device_list_cache(
        self, user_id: str, devices: List[dict], stream_id: int
    ) -> None:
        """Replace the entire cache of the remote user's devices.

        Note: assumes that we are the only thread that can be updating this user's
        device list.

        Args:
            user_id: User to update device list for
            devices: list of device objects supplied over federation
            stream_id: the version of the device list
        """
        await self.db_pool.runInteraction(
            "update_remote_device_list_cache",
            self._update_remote_device_list_cache_txn,
            user_id,
            devices,
            stream_id,
        )

    def _update_remote_device_list_cache_txn(
        self, txn: LoggingTransaction, user_id: str, devices: List[dict], stream_id: int
    ) -> None:
        """Replace the list of cached devices for this user with the given list."""
        self.db_pool.simple_delete_txn(
            txn, table="device_lists_remote_cache", keyvalues={"user_id": user_id}
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            table="device_lists_remote_cache",
            keys=("user_id", "device_id", "content"),
            values=[
                (user_id, content["device_id"], json_encoder.encode(content))
                for content in devices
            ],
        )

        txn.call_after(self.get_cached_devices_for_user.invalidate, (user_id,))
        txn.call_after(self._get_cached_user_device.invalidate, (user_id,))
        txn.call_after(
            self.get_device_list_last_stream_id_for_remote.invalidate, (user_id,)
        )

        self.db_pool.simple_upsert_txn(
            txn,
            table="device_lists_remote_extremeties",
            keyvalues={"user_id": user_id},
            values={"stream_id": stream_id},
        )

    async def add_device_change_to_streams(
        self,
        user_id: str,
        device_ids: StrCollection,
        room_ids: StrCollection,
    ) -> Optional[int]:
        """Persist that a user's devices have been updated, and which hosts
        (if any) should be poked.

        Args:
            user_id: The ID of the user whose device changed.
            device_ids: The IDs of any changed devices. If empty, this function will
                return None.
            room_ids: The rooms that the user is in

        Returns:
            The maximum stream ID of device list updates that were added to the database, or
            None if no updates were added.
        """
        if not device_ids:
            return None

        context = get_active_span_text_map()

        def add_device_changes_txn(
            txn: LoggingTransaction, stream_ids: List[int]
        ) -> None:
            self._add_device_change_to_stream_txn(
                txn,
                user_id,
                device_ids,
                stream_ids,
            )

            self._add_device_outbound_room_poke_txn(
                txn,
                user_id,
                device_ids,
                room_ids,
                stream_ids,
                context,
            )

        async with self._device_list_id_gen.get_next_mult(
            len(device_ids)
        ) as stream_ids:
            await self.db_pool.runInteraction(
                "add_device_change_to_stream",
                add_device_changes_txn,
                stream_ids,
            )

        return stream_ids[-1]

    def _add_device_change_to_stream_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_ids: Collection[str],
        stream_ids: List[int],
    ) -> None:
        txn.call_after(
            self._device_list_stream_cache.entity_has_changed,
            user_id,
            stream_ids[-1],
        )
        txn.call_after(
            self._get_e2e_device_keys_for_federation_query_inner.invalidate,
            (user_id,),
        )

        min_stream_id = stream_ids[0]

        # Delete older entries in the table, as we really only care about
        # when the latest change happened.
        cleanup_obsolete_stmt = """
            DELETE FROM device_lists_stream
            WHERE user_id = ? AND stream_id < ? AND %s
        """
        device_ids_clause, device_ids_args = make_in_list_sql_clause(
            txn.database_engine, "device_id", device_ids
        )
        txn.execute(
            cleanup_obsolete_stmt % (device_ids_clause,),
            [user_id, min_stream_id] + device_ids_args,
        )

        self.db_pool.simple_insert_many_txn(
            txn,
            table="device_lists_stream",
            keys=("instance_name", "stream_id", "user_id", "device_id"),
            values=[
                (self._instance_name, stream_id, user_id, device_id)
                for stream_id, device_id in zip(stream_ids, device_ids)
            ],
        )

    def _add_device_outbound_poke_to_stream_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_id: str,
        hosts: Collection[str],
        stream_id: int,
        context: Optional[Dict[str, str]],
    ) -> None:
        if self._device_list_federation_stream_cache:
            for host in hosts:
                txn.call_after(
                    self._device_list_federation_stream_cache.entity_has_changed,
                    host,
                    stream_id,
                )

        now = self._clock.time_msec()

        encoded_context = json_encoder.encode(context)
        mark_sent = not self.hs.is_mine_id(user_id)

        values = [
            (
                destination,
                self._instance_name,
                stream_id,
                user_id,
                device_id,
                mark_sent,
                now,
                encoded_context if whitelisted_homeserver(destination) else "{}",
            )
            for destination in hosts
        ]

        self.db_pool.simple_insert_many_txn(
            txn,
            table="device_lists_outbound_pokes",
            keys=(
                "destination",
                "instance_name",
                "stream_id",
                "user_id",
                "device_id",
                "sent",
                "ts",
                "opentracing_context",
            ),
            values=values,
        )

        # debugging for https://github.com/matrix-org/synapse/issues/14251
        if issue_8631_logger.isEnabledFor(logging.DEBUG):
            issue_8631_logger.debug(
                "Recorded outbound pokes for %s:%s with device stream ids %s",
                user_id,
                device_id,
                {
                    stream_id: destination
                    for (destination, _, stream_id, _, _, _, _, _) in values
                },
            )

    async def mark_redundant_device_lists_pokes(
        self,
        user_id: str,
        device_id: str,
        room_id: str,
        converted_upto_stream_id: int,
    ) -> None:
        """If we've calculated the outbound pokes for a given room/device list
        update, mark any subsequent changes as already converted"""

        sql = """
            UPDATE device_lists_changes_in_room
            SET converted_to_destinations = true
            WHERE stream_id > ? AND user_id = ? AND device_id = ?
                AND room_id = ? AND NOT converted_to_destinations
        """

        def mark_redundant_device_lists_pokes_txn(txn: LoggingTransaction) -> None:
            txn.execute(sql, (converted_upto_stream_id, user_id, device_id, room_id))

        return await self.db_pool.runInteraction(
            "mark_redundant_device_lists_pokes", mark_redundant_device_lists_pokes_txn
        )

    def _add_device_outbound_room_poke_txn(
        self,
        txn: LoggingTransaction,
        user_id: str,
        device_ids: StrCollection,
        room_ids: StrCollection,
        stream_ids: List[int],
        context: Dict[str, str],
    ) -> None:
        """Record the user in the room has updated their device."""

        encoded_context = json_encoder.encode(context)

        # The `device_lists_changes_in_room.stream_id` column matches the
        # corresponding `stream_id` of the update in the `device_lists_stream`
        # table, i.e. all rows persisted for the same device update will have
        # the same `stream_id` (but different room IDs).
        self.db_pool.simple_insert_many_txn(
            txn,
            table="device_lists_changes_in_room",
            keys=(
                "user_id",
                "device_id",
                "room_id",
                "stream_id",
                "instance_name",
                "converted_to_destinations",
                "opentracing_context",
            ),
            values=[
                (
                    user_id,
                    device_id,
                    room_id,
                    stream_id,
                    self._instance_name,
                    # We only need to calculate outbound pokes for local users
                    not self.hs.is_mine_id(user_id),
                    encoded_context,
                )
                for room_id in room_ids
                for device_id, stream_id in zip(device_ids, stream_ids)
            ],
        )

        txn.call_after(
            self.device_lists_in_rooms_have_changed, room_ids, max(stream_ids)
        )

    async def get_uncoverted_outbound_room_pokes(
        self, start_stream_id: int, start_room_id: str, limit: int = 10
    ) -> List[Tuple[str, str, str, int, Optional[Dict[str, str]]]]:
        """Get device list changes by room that have not yet been handled and
        written to `device_lists_outbound_pokes`.

        Args:
            start_stream_id: Together with `start_room_id`, indicates the position after
                which to return device list changes.
            start_room_id: Together with `start_stream_id`, indicates the position after
                which to return device list changes.
            limit: The maximum number of device list changes to return.

        Returns:
            A list of user ID, device ID, room ID, stream ID and optional opentracing
            context, in order of ascending (stream ID, room ID).
        """

        sql = """
            SELECT user_id, device_id, room_id, stream_id, opentracing_context
            FROM device_lists_changes_in_room
            WHERE
                (stream_id, room_id) > (?, ?) AND
                stream_id <= ? AND
                NOT converted_to_destinations
            ORDER BY stream_id ASC, room_id ASC
            LIMIT ?
        """

        def get_uncoverted_outbound_room_pokes_txn(
            txn: LoggingTransaction,
        ) -> List[Tuple[str, str, str, int, Optional[Dict[str, str]]]]:
            txn.execute(
                sql,
                (
                    start_stream_id,
                    start_room_id,
                    # Avoid returning rows if there may be uncommitted device list
                    # changes with smaller stream IDs.
                    self._device_list_id_gen.get_current_token(),
                    limit,
                ),
            )

            return [
                (
                    user_id,
                    device_id,
                    room_id,
                    stream_id,
                    db_to_json(opentracing_context),
                )
                for user_id, device_id, room_id, stream_id, opentracing_context in txn
            ]

        return await self.db_pool.runInteraction(
            "get_uncoverted_outbound_room_pokes", get_uncoverted_outbound_room_pokes_txn
        )

    async def add_device_list_outbound_pokes(
        self,
        user_id: str,
        device_id: str,
        room_id: str,
        hosts: Collection[str],
        context: Optional[Dict[str, str]],
    ) -> None:
        """Queue the device update to be sent to the given set of hosts,
        calculated from the room ID.
        """
        if not hosts:
            return

        def add_device_list_outbound_pokes_txn(
            txn: LoggingTransaction, stream_id: int
        ) -> None:
            self._add_device_outbound_poke_to_stream_txn(
                txn,
                user_id=user_id,
                device_id=device_id,
                hosts=hosts,
                stream_id=stream_id,
                context=context,
            )

        async with self._device_list_id_gen.get_next() as stream_id:
            return await self.db_pool.runInteraction(
                "add_device_list_outbound_pokes",
                add_device_list_outbound_pokes_txn,
                stream_id,
            )

    async def add_remote_device_list_to_pending(
        self, user_id: str, device_id: str
    ) -> None:
        """Add a device list update to the table tracking remote device list
        updates during partial joins.
        """

        async with self._device_list_id_gen.get_next() as stream_id:
            await self.db_pool.simple_upsert(
                table="device_lists_remote_pending",
                keyvalues={
                    "user_id": user_id,
                    "device_id": device_id,
                },
                values={
                    "stream_id": stream_id,
                    "instance_name": self._instance_name,
                },
                desc="add_remote_device_list_to_pending",
            )

    async def get_pending_remote_device_list_updates_for_room(
        self, room_id: str
    ) -> Collection[Tuple[str, str]]:
        """Get the set of remote device list updates from the pending table for
        the room.
        """

        min_device_stream_id = await self.db_pool.simple_select_one_onecol(
            table="partial_state_rooms",
            keyvalues={
                "room_id": room_id,
            },
            retcol="device_lists_stream_id",
            desc="get_pending_remote_device_list_updates_for_room_device",
        )

        sql = """
            SELECT user_id, device_id FROM device_lists_remote_pending AS d
            INNER JOIN current_state_events AS c ON
                type = 'm.room.member'
                AND state_key = user_id
                AND membership = 'join'
            WHERE
                room_id = ? AND stream_id > ?
        """

        def get_pending_remote_device_list_updates_for_room_txn(
            txn: LoggingTransaction,
        ) -> Collection[Tuple[str, str]]:
            txn.execute(sql, (room_id, min_device_stream_id))
            return cast(Collection[Tuple[str, str]], txn.fetchall())

        return await self.db_pool.runInteraction(
            "get_pending_remote_device_list_updates_for_room",
            get_pending_remote_device_list_updates_for_room_txn,
        )

    async def get_device_change_last_converted_pos(self) -> Tuple[int, str]:
        """
        Get the position of the last row in `device_list_changes_in_room` that has been
        converted to `device_lists_outbound_pokes`.

        Rows with a strictly greater position where `converted_to_destinations` is
        `FALSE` have not been converted.
        """

        # There should be only one row in this table, though we want to
        # future-proof ourselves for when we have multiple rows (one for each
        # instance). So to handle that case we take the minimum of all rows.
        rows = await self.db_pool.simple_select_list(
            table="device_lists_changes_converted_stream_position",
            keyvalues={},
            retcols=["stream_id", "room_id"],
            desc="get_device_change_last_converted_pos",
        )
        return cast(Tuple[int, str], min(rows))

    async def set_device_change_last_converted_pos(
        self,
        stream_id: int,
        room_id: str,
    ) -> None:
        """
        Set the position of the last row in `device_list_changes_in_room` that has been
        converted to `device_lists_outbound_pokes`.
        """

        await self.db_pool.simple_update_one(
            table="device_lists_changes_converted_stream_position",
            keyvalues={},
            updatevalues={
                "stream_id": stream_id,
                "instance_name": self._instance_name,
                "room_id": room_id,
            },
            desc="set_device_change_last_converted_pos",
        )
