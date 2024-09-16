#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Optional, Tuple

import attr
from canonicaljson import encode_canonical_json
from signedjson.key import VerifyKey, decode_verify_key_bytes
from signedjson.sign import SignatureVerifyException, verify_signed_json
from unpaddedbase64 import decode_base64

from twisted.internet import defer

from synapse.api.constants import EduTypes
from synapse.api.errors import CodeMessageException, Codes, NotFoundError, SynapseError
from synapse.handlers.device import DeviceHandler
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.logging.opentracing import log_kv, set_tag, tag_args, trace
from synapse.replication.http.devices import ReplicationUploadKeysForUserRestServlet
from synapse.types import (
    JsonDict,
    JsonMapping,
    UserID,
    get_domain_from_id,
    get_verify_key_from_cross_signing_key,
)
from synapse.util import json_decoder
from synapse.util.async_helpers import Linearizer, concurrently_execute
from synapse.util.cancellation import cancellable
from synapse.util.retryutils import (
    NotRetryingDestination,
    filter_destinations_by_retry_limiter,
)

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


ONE_TIME_KEY_UPLOAD = "one_time_key_upload_lock"


class E2eKeysHandler:
    def __init__(self, hs: "HomeServer"):
        self.config = hs.config
        self.store = hs.get_datastores().main
        self.federation = hs.get_federation_client()
        self.device_handler = hs.get_device_handler()
        self._appservice_handler = hs.get_application_service_handler()
        self.is_mine = hs.is_mine
        self.clock = hs.get_clock()
        self._worker_lock_handler = hs.get_worker_locks_handler()

        federation_registry = hs.get_federation_registry()

        is_master = hs.config.worker.worker_app is None
        if is_master:
            edu_updater = SigningKeyEduUpdater(hs)

            # Only register this edu handler on master as it requires writing
            # device updates to the db
            federation_registry.register_edu_handler(
                EduTypes.SIGNING_KEY_UPDATE,
                edu_updater.incoming_signing_key_update,
            )
            # also handle the unstable version
            # FIXME: remove this when enough servers have upgraded
            federation_registry.register_edu_handler(
                EduTypes.UNSTABLE_SIGNING_KEY_UPDATE,
                edu_updater.incoming_signing_key_update,
            )

            self.device_key_uploader = self.upload_device_keys_for_user
        else:
            self.device_key_uploader = (
                ReplicationUploadKeysForUserRestServlet.make_client(hs)
            )

        # doesn't really work as part of the generic query API, because the
        # query request requires an object POST, but we abuse the
        # "query handler" interface.
        federation_registry.register_query_handler(
            "client_keys", self.on_federation_query_client_keys
        )

        # Limit the number of in-flight requests from a single device.
        self._query_devices_linearizer = Linearizer(
            name="query_devices",
            max_count=10,
        )

        self._query_appservices_for_otks = (
            hs.config.experimental.msc3983_appservice_otk_claims
        )
        self._query_appservices_for_keys = (
            hs.config.experimental.msc3984_appservice_key_query
        )

    @trace
    @cancellable
    async def query_devices(
        self,
        query_body: JsonDict,
        timeout: int,
        from_user_id: str,
        from_device_id: Optional[str],
    ) -> JsonDict:
        """Handle a device key query from a client

        {
            "device_keys": {
                "<user_id>": ["<device_id>"]
            }
        }
        ->
        {
            "device_keys": {
                "<user_id>": {
                    "<device_id>": {
                        ...
                    }
                }
            }
        }

        Args:
            from_user_id: the user making the query.  This is used when
                adding cross-signing signatures to limit what signatures users
                can see.
            from_device_id: the device making the query. This is used to limit
                the number of in-flight queries at a time.
        """
        async with self._query_devices_linearizer.queue((from_user_id, from_device_id)):
            device_keys_query: Dict[str, List[str]] = query_body.get("device_keys", {})

            # separate users by domain.
            # make a map from domain to user_id to device_ids
            local_query = {}
            remote_queries = {}

            for user_id, device_ids in device_keys_query.items():
                if not UserID.is_valid(user_id):
                    # Ignore invalid user IDs, which is the same behaviour as if
                    # the user existed but had no keys.
                    continue

                # we use UserID.from_string to catch invalid user ids
                if self.is_mine(UserID.from_string(user_id)):
                    local_query[user_id] = device_ids
                else:
                    remote_queries[user_id] = device_ids

            set_tag("local_key_query", str(local_query))
            set_tag("remote_key_query", str(remote_queries))

            # First get local devices.
            # A map of destination -> failure response.
            failures: Dict[str, JsonDict] = {}
            results = {}
            if local_query:
                local_result = await self.query_local_devices(local_query)
                for user_id, keys in local_result.items():
                    if user_id in local_query:
                        results[user_id] = keys

            # Get cached cross-signing keys
            cross_signing_keys = await self.get_cross_signing_keys_from_cache(
                device_keys_query, from_user_id
            )

            # Now attempt to get any remote devices from our local cache.
            # A map of destination -> user ID -> device IDs.
            remote_queries_not_in_cache: Dict[str, Dict[str, Iterable[str]]] = {}
            if remote_queries:
                user_ids = set()
                user_and_device_ids: List[Tuple[str, str]] = []
                for user_id, device_ids in remote_queries.items():
                    if device_ids:
                        user_and_device_ids.extend(
                            (user_id, device_id) for device_id in device_ids
                        )
                    else:
                        user_ids.add(user_id)

                (
                    user_ids_not_in_cache,
                    remote_results,
                ) = await self.store.get_user_devices_from_cache(
                    user_ids, user_and_device_ids
                )

                # Check that the homeserver still shares a room with all cached users.
                # Note that this check may be slightly racy when a remote user leaves a
                # room after we have fetched their cached device list. In the worst case
                # we will do extra federation queries for devices that we had cached.
                cached_users = set(remote_results.keys())
                valid_cached_users = (
                    await self.store.get_users_server_still_shares_room_with(
                        remote_results.keys()
                    )
                )
                invalid_cached_users = cached_users - valid_cached_users
                if invalid_cached_users:
                    # Fix up results. If we get here, it means there was either a bug in
                    # device list tracking, or we hit the race mentioned above.
                    # TODO: In practice, this path is hit fairly often in existing
                    #       deployments when clients query the keys of departed remote
                    #       users. A background update to mark the appropriate device
                    #       lists as unsubscribed is needed.
                    #       https://github.com/matrix-org/synapse/issues/13651
                    # Note that this currently introduces a failure mode when clients
                    # are trying to decrypt old messages from a remote user whose
                    # homeserver is no longer available. We may want to consider falling
                    # back to the cached data when we fail to retrieve a device list
                    # over federation for such remote users.
                    user_ids_not_in_cache.update(invalid_cached_users)
                    for invalid_user_id in invalid_cached_users:
                        remote_results.pop(invalid_user_id)

                for user_id, devices in remote_results.items():
                    user_devices = results.setdefault(user_id, {})
                    for device_id, device in devices.items():
                        keys = device.get("keys", None)
                        device_display_name = device.get("device_display_name", None)
                        if keys:
                            result = dict(keys)
                            unsigned = result.setdefault("unsigned", {})
                            if device_display_name:
                                unsigned["device_display_name"] = device_display_name
                            user_devices[device_id] = result

                # check for missing cross-signing keys.
                for user_id in remote_queries.keys():
                    cached_cross_master = user_id in cross_signing_keys["master_keys"]
                    cached_cross_selfsigning = (
                        user_id in cross_signing_keys["self_signing_keys"]
                    )

                    # check if we are missing only one of cross-signing master or
                    # self-signing key, but the other one is cached.
                    # as we need both, this will issue a federation request.
                    # if we don't have any of the keys, either the user doesn't have
                    # cross-signing set up, or the cached device list
                    # is not (yet) updated.
                    if cached_cross_master ^ cached_cross_selfsigning:
                        user_ids_not_in_cache.add(user_id)

                # add those users to the list to fetch over federation.
                for user_id in user_ids_not_in_cache:
                    domain = get_domain_from_id(user_id)
                    r = remote_queries_not_in_cache.setdefault(domain, {})
                    r[user_id] = remote_queries[user_id]

            # Now fetch any devices that we don't have in our cache
            # TODO It might make sense to propagate cancellations into the
            #      deferreds which are querying remote homeservers.
            logger.debug(
                "%d destinations to query devices for", len(remote_queries_not_in_cache)
            )

            async def _query(destination: str) -> None:
                queries = remote_queries_not_in_cache[destination]
                return await self._query_devices_for_destination(
                    results,
                    cross_signing_keys,
                    failures,
                    destination,
                    queries,
                    timeout,
                )

            # Only try and fetch keys for destinations that are not marked as
            # down.
            unfiltered_destinations = remote_queries_not_in_cache.keys()
            filtered_destinations = set(
                await filter_destinations_by_retry_limiter(
                    unfiltered_destinations,
                    self.clock,
                    self.store,
                    # Let's give an arbitrary grace period for those hosts that are
                    # only recently down
                    retry_due_within_ms=60 * 1000,
                )
            )
            failures.update(
                (dest, _NOT_READY_FOR_RETRY_FAILURE)
                for dest in (unfiltered_destinations - filtered_destinations)
            )

            await concurrently_execute(
                _query,
                filtered_destinations,
                10,
                delay_cancellation=True,
            )

            return {"device_keys": results, "failures": failures, **cross_signing_keys}

    @trace
    async def _query_devices_for_destination(
        self,
        results: JsonDict,
        cross_signing_keys: JsonDict,
        failures: Dict[str, JsonDict],
        destination: str,
        destination_query: Dict[str, Iterable[str]],
        timeout: int,
    ) -> None:
        """This is called when we are querying the device list of a user on
        a remote homeserver and their device list is not in the device list
        cache. If we share a room with this user and we're not querying for
        specific user we will update the cache with their device list.

        Args:
            results: A map from user ID to their device keys, which gets
                updated with the newly fetched keys.
            cross_signing_keys: Map from user ID to their cross signing keys,
                which gets updated with the newly fetched keys.
            failures: Map of destinations to failures that have occurred while
                attempting to fetch keys.
            destination: The remote server to query
            destination_query: The query dict of devices to query the remote
                server for.
            timeout: The timeout for remote HTTP requests.
        """

        # We first consider whether we wish to update the device list cache with
        # the users device list. We want to track a user's devices when the
        # authenticated user shares a room with the queried user and the query
        # has not specified a particular device.
        # If we update the cache for the queried user we remove them from further
        # queries. We use the more efficient batched query_client_keys for all
        # remaining users
        user_ids_updated = []

        # Perform a user device resync for each user only once and only as long as:
        # - they have an empty device_list
        # - they are in some rooms that this server can see
        users_to_resync_devices = {
            user_id
            for (user_id, device_list) in destination_query.items()
            if (not device_list) and (await self.store.get_rooms_for_user(user_id))
        }

        logger.debug(
            "%d users to resync devices for from destination %s",
            len(users_to_resync_devices),
            destination,
        )

        try:
            user_resync_results = (
                await self.device_handler.device_list_updater.multi_user_device_resync(
                    list(users_to_resync_devices)
                )
            )
            for user_id in users_to_resync_devices:
                resync_results = user_resync_results[user_id]

                if resync_results is None:
                    # TODO: It's weird that we'll store a failure against a
                    #       destination, yet continue processing users from that
                    #       destination.
                    #       We might want to consider changing this, but for now
                    #       I'm leaving it as I found it.
                    failures[destination] = _exception_to_failure(
                        ValueError(f"Device resync failed for {user_id!r}")
                    )
                    continue

                # Add the device keys to the results.
                user_devices = resync_results["devices"]
                user_results = results.setdefault(user_id, {})
                for device in user_devices:
                    user_results[device["device_id"]] = device["keys"]
                user_ids_updated.append(user_id)

                # Add any cross signing keys to the results.
                master_key = resync_results.get("master_key")
                self_signing_key = resync_results.get("self_signing_key")

                if master_key:
                    cross_signing_keys["master_keys"][user_id] = master_key

                if self_signing_key:
                    cross_signing_keys["self_signing_keys"][user_id] = self_signing_key
        except Exception as e:
            failures[destination] = _exception_to_failure(e)

        if len(destination_query) == len(user_ids_updated):
            # We've updated all the users in the query and we do not need to
            # make any further remote calls.
            return

        # Remove all the users from the query which we have updated
        for user_id in user_ids_updated:
            destination_query.pop(user_id)

        try:
            remote_result = await self.federation.query_client_keys(
                destination, {"device_keys": destination_query}, timeout=timeout
            )

            for user_id, keys in remote_result["device_keys"].items():
                if user_id in destination_query:
                    results[user_id] = keys

            if "master_keys" in remote_result:
                for user_id, key in remote_result["master_keys"].items():
                    if user_id in destination_query:
                        cross_signing_keys["master_keys"][user_id] = key

            if "self_signing_keys" in remote_result:
                for user_id, key in remote_result["self_signing_keys"].items():
                    if user_id in destination_query:
                        cross_signing_keys["self_signing_keys"][user_id] = key

        except Exception as e:
            failure = _exception_to_failure(e)
            failures[destination] = failure
            set_tag("error", True)
            set_tag("reason", str(failure))

        return

    @cancellable
    async def get_cross_signing_keys_from_cache(
        self, query: Iterable[str], from_user_id: Optional[str]
    ) -> Dict[str, Dict[str, JsonMapping]]:
        """Get cross-signing keys for users from the database

        Args:
            query: an iterable of user IDs.  A dict whose keys
                are user IDs satisfies this, so the query format used for
                query_devices can be used here.
            from_user_id: the user making the query.  This is used when
                adding cross-signing signatures to limit what signatures users
                can see.

        Returns:
            A map from (master_keys|self_signing_keys|user_signing_keys) -> user_id -> key
        """
        master_keys = {}
        self_signing_keys = {}
        user_signing_keys = {}

        user_ids = list(query)

        keys = await self.store.get_e2e_cross_signing_keys_bulk(user_ids, from_user_id)

        for user_id, user_info in keys.items():
            if user_info is None:
                continue
            if "master" in user_info:
                master_keys[user_id] = user_info["master"]
            if "self_signing" in user_info:
                self_signing_keys[user_id] = user_info["self_signing"]

        # users can see other users' master and self-signing keys, but can
        # only see their own user-signing keys
        if from_user_id:
            from_user_key = keys.get(from_user_id)
            if from_user_key and "user_signing" in from_user_key:
                user_signing_keys[from_user_id] = from_user_key["user_signing"]

        return {
            "master_keys": master_keys,
            "self_signing_keys": self_signing_keys,
            "user_signing_keys": user_signing_keys,
        }

    @trace
    @cancellable
    async def query_local_devices(
        self,
        query: Mapping[str, Optional[List[str]]],
        include_displaynames: bool = True,
    ) -> Dict[str, Dict[str, dict]]:
        """Get E2E device keys for local users

        Args:
            query: map from user_id to a list
                 of devices to query (None for all devices)
            include_displaynames: Whether to include device displaynames in the returned
                device details.

        Returns:
            A map from user_id -> device_id -> device details
        """
        set_tag("local_query", str(query))
        local_query: List[Tuple[str, Optional[str]]] = []

        result_dict: Dict[str, Dict[str, dict]] = {}
        for user_id, device_ids in query.items():
            # we use UserID.from_string to catch invalid user ids
            if not self.is_mine(UserID.from_string(user_id)):
                logger.warning("Request for keys for non-local user %s", user_id)
                log_kv(
                    {
                        "message": "Requested a local key for a user which"
                        " was not local to the homeserver",
                        "user_id": user_id,
                    }
                )
                set_tag("error", True)
                raise SynapseError(400, "Not a user here")

            if not device_ids:
                local_query.append((user_id, None))
            else:
                for device_id in device_ids:
                    local_query.append((user_id, device_id))

            # make sure that each queried user appears in the result dict
            result_dict[user_id] = {}

        results = await self.store.get_e2e_device_keys_for_cs_api(
            local_query, include_displaynames
        )

        # Check if the application services have any additional results.
        if self._query_appservices_for_keys:
            # Query the appservices for any keys.
            appservice_results = await self._appservice_handler.query_keys(query)

            # Merge results, overriding with what the appservice returned.
            for user_id, devices in appservice_results.get("device_keys", {}).items():
                # Copy the appservice device info over the homeserver device info, but
                # don't completely overwrite it.
                results.setdefault(user_id, {}).update(devices)

            # TODO Handle cross-signing keys.

        # Build the result structure
        for user_id, device_keys in results.items():
            for device_id, device_info in device_keys.items():
                result_dict[user_id][device_id] = device_info

        log_kv(results)
        return result_dict

    async def on_federation_query_client_keys(
        self, query_body: Dict[str, Dict[str, Optional[List[str]]]]
    ) -> JsonDict:
        """Handle a device key query from a federated server:

        Handles the path: GET /_matrix/federation/v1/users/keys/query

        Args:
            query_body: The body of the query request. Should contain a key
                "device_keys" that map to a dictionary of user ID's -> list of
                device IDs. If the list of device IDs is empty, all devices of
                that user will be queried.

        Returns:
            A json dictionary containing the following:
                - device_keys: A dictionary containing the requested device information.
                - master_keys: An optional dictionary of user ID -> master cross-signing
                   key info.
                - self_signing_key: An optional dictionary of user ID -> self-signing
                    key info.
        """
        device_keys_query: Dict[str, Optional[List[str]]] = query_body.get(
            "device_keys", {}
        )
        if any(
            not self.is_mine(UserID.from_string(user_id))
            for user_id in device_keys_query
        ):
            raise SynapseError(400, "User is not hosted on this homeserver")

        res = await self.query_local_devices(
            device_keys_query,
            include_displaynames=(
                self.config.federation.allow_device_name_lookup_over_federation
            ),
        )

        # add in the cross-signing keys
        cross_signing_keys = await self.get_cross_signing_keys_from_cache(
            device_keys_query, None
        )

        return {"device_keys": res, **cross_signing_keys}

    async def claim_local_one_time_keys(
        self,
        local_query: List[Tuple[str, str, str, int]],
        always_include_fallback_keys: bool,
    ) -> Iterable[Dict[str, Dict[str, Dict[str, JsonDict]]]]:
        """Claim one time keys for local users.

        1. Attempt to claim OTKs from the database.
        2. Ask application services if they provide OTKs.
        3. Attempt to fetch fallback keys from the database.

        Args:
            local_query: An iterable of tuples of (user ID, device ID, algorithm).
            always_include_fallback_keys: True to always include fallback keys.

        Returns:
            An iterable of maps of user ID -> a map device ID -> a map of key ID -> JSON bytes.
        """

        # Cap the number of OTKs that can be claimed at once to avoid abuse.
        local_query = [
            (user_id, device_id, algorithm, min(count, 5))
            for user_id, device_id, algorithm, count in local_query
        ]

        otk_results, not_found = await self.store.claim_e2e_one_time_keys(local_query)

        # If the application services have not provided any keys via the C-S
        # API, query it directly for one-time keys.
        if self._query_appservices_for_otks:
            # TODO Should this query for fallback keys of uploaded OTKs if
            #      always_include_fallback_keys is True? The MSC is ambiguous.
            (
                appservice_results,
                not_found,
            ) = await self._appservice_handler.claim_e2e_one_time_keys(not_found)
        else:
            appservice_results = {}

        # Calculate which user ID / device ID / algorithm tuples to get fallback
        # keys for. This can be either only missing results *or* all results
        # (which don't already have a fallback key).
        if always_include_fallback_keys:
            # Build the fallback query as any part of the original query where
            # the appservice didn't respond with a fallback key.
            fallback_query = []

            # Iterate each item in the original query and search the results
            # from the appservice for that user ID / device ID. If it is found,
            # check if any of the keys match the requested algorithm & are a
            # fallback key.
            for user_id, device_id, algorithm, _count in local_query:
                # Check if the appservice responded for this query.
                as_result = appservice_results.get(user_id, {}).get(device_id, {})
                found_otk = False
                for key_id, key_json in as_result.items():
                    if key_id.startswith(f"{algorithm}:"):
                        # A OTK or fallback key was found for this query.
                        found_otk = True
                        # A fallback key was found for this query, no need to
                        # query further.
                        if key_json.get("fallback", False):
                            break

                else:
                    # No fallback key was found from appservices, query for it.
                    # Only mark the fallback key as used if no OTK was found
                    # (from either the database or appservices).
                    mark_as_used = not found_otk and not any(
                        key_id.startswith(f"{algorithm}:")
                        for key_id in otk_results.get(user_id, {})
                        .get(device_id, {})
                        .keys()
                    )
                    # Note that it doesn't make sense to request more than 1 fallback key
                    # per (user_id, device_id, algorithm).
                    fallback_query.append((user_id, device_id, algorithm, mark_as_used))

        else:
            # All fallback keys get marked as used.
            fallback_query = [
                # Note that it doesn't make sense to request more than 1 fallback key
                # per (user_id, device_id, algorithm).
                (user_id, device_id, algorithm, True)
                for user_id, device_id, algorithm, count in not_found
            ]

        # For each user that does not have a one-time keys available, see if
        # there is a fallback key.
        fallback_results = await self.store.claim_e2e_fallback_keys(fallback_query)

        # Return the results in order, each item from the input query should
        # only appear once in the combined list.
        return (otk_results, appservice_results, fallback_results)

    @trace
    async def claim_one_time_keys(
        self,
        query: Dict[str, Dict[str, Dict[str, int]]],
        user: UserID,
        timeout: Optional[int],
        always_include_fallback_keys: bool,
    ) -> JsonDict:
        """
        Args:
            query: A chain of maps from (user_id, device_id, algorithm) to the requested
                number of keys to claim.
            user: The user who is claiming these keys.
            timeout: How long to wait for any federation key claim requests before
                giving up.
            always_include_fallback_keys: always include a fallback key for local users'
                devices, even if we managed to claim a one-time-key.

        Returns: a heterogeneous dict with two keys:
            one_time_keys: chain of maps user ID -> device ID -> key ID -> key.
            failures: map from remote destination to a JsonDict describing the error.
        """
        local_query: List[Tuple[str, str, str, int]] = []
        remote_queries: Dict[str, Dict[str, Dict[str, Dict[str, int]]]] = {}

        for user_id, one_time_keys in query.items():
            # we use UserID.from_string to catch invalid user ids
            if self.is_mine(UserID.from_string(user_id)):
                for device_id, algorithms in one_time_keys.items():
                    for algorithm, count in algorithms.items():
                        local_query.append((user_id, device_id, algorithm, count))
            else:
                domain = get_domain_from_id(user_id)
                remote_queries.setdefault(domain, {})[user_id] = one_time_keys

        set_tag("local_key_query", str(local_query))
        set_tag("remote_key_query", str(remote_queries))

        results = await self.claim_local_one_time_keys(
            local_query, always_include_fallback_keys
        )

        # A map of user ID -> device ID -> key ID -> key.
        json_result: Dict[str, Dict[str, Dict[str, JsonDict]]] = {}
        for result in results:
            for user_id, device_keys in result.items():
                for device_id, keys in device_keys.items():
                    for key_id, key in keys.items():
                        json_result.setdefault(user_id, {}).setdefault(
                            device_id, {}
                        ).update({key_id: key})

        # Remote failures.
        failures: Dict[str, JsonDict] = {}

        @trace
        async def claim_client_keys(destination: str) -> None:
            set_tag("destination", destination)
            device_keys = remote_queries[destination]
            try:
                remote_result = await self.federation.claim_client_keys(
                    user, destination, device_keys, timeout=timeout
                )
                for user_id, keys in remote_result["one_time_keys"].items():
                    if user_id in device_keys:
                        json_result[user_id] = keys

            except Exception as e:
                failure = _exception_to_failure(e)
                failures[destination] = failure
                set_tag("error", True)
                set_tag("reason", str(failure))

        await make_deferred_yieldable(
            defer.gatherResults(
                [
                    run_in_background(claim_client_keys, destination)
                    for destination in remote_queries
                ],
                consumeErrors=True,
            )
        )

        logger.info(
            "Claimed one-time-keys: %s",
            ",".join(
                (
                    "%s for %s:%s" % (key_id, user_id, device_id)
                    for user_id, user_keys in json_result.items()
                    for device_id, device_keys in user_keys.items()
                    for key_id, _ in device_keys.items()
                )
            ),
        )

        log_kv({"one_time_keys": json_result, "failures": failures})
        return {"one_time_keys": json_result, "failures": failures}

    @tag_args
    async def upload_keys_for_user(
        self, user_id: str, device_id: str, keys: JsonDict
    ) -> JsonDict:
        """
        Args:
            user_id: user whose keys are being uploaded.
            device_id: device whose keys are being uploaded.
            keys: the body of a /keys/upload request.

        Returns a dictionary with one field:
            "one_time_keys": A mapping from algorithm to number of keys for that
                algorithm, including those previously persisted.
        """
        time_now = self.clock.time_msec()

        # TODO: Validate the JSON to make sure it has the right keys.
        device_keys = keys.get("device_keys", None)
        if device_keys:
            await self.device_key_uploader(
                user_id=user_id,
                device_id=device_id,
                keys={"device_keys": device_keys},
            )

        one_time_keys = keys.get("one_time_keys", None)
        if one_time_keys:
            log_kv(
                {
                    "message": "Updating one_time_keys for device.",
                    "user_id": user_id,
                    "device_id": device_id,
                }
            )
            await self._upload_one_time_keys_for_user(
                user_id, device_id, time_now, one_time_keys
            )
        else:
            log_kv(
                {"message": "Did not update one_time_keys", "reason": "no keys given"}
            )
        fallback_keys = keys.get("fallback_keys") or keys.get(
            "org.matrix.msc2732.fallback_keys"
        )
        if fallback_keys and isinstance(fallback_keys, dict):
            log_kv(
                {
                    "message": "Updating fallback_keys for device.",
                    "user_id": user_id,
                    "device_id": device_id,
                }
            )
            await self.store.set_e2e_fallback_keys(user_id, device_id, fallback_keys)
        elif fallback_keys:
            log_kv({"message": "Did not update fallback_keys", "reason": "not a dict"})
        else:
            log_kv(
                {"message": "Did not update fallback_keys", "reason": "no keys given"}
            )

        result = await self.store.count_e2e_one_time_keys(user_id, device_id)

        set_tag("one_time_key_counts", str(result))
        return {"one_time_key_counts": result}

    @tag_args
    async def upload_device_keys_for_user(
        self, user_id: str, device_id: str, keys: JsonDict
    ) -> None:
        """
        Args:
            user_id: user whose keys are being uploaded.
            device_id: device whose keys are being uploaded.
            device_keys: the `device_keys` of an /keys/upload request.

        """
        # This can only be called from the main process.
        assert isinstance(self.device_handler, DeviceHandler)

        time_now = self.clock.time_msec()

        device_keys = keys["device_keys"]
        logger.info(
            "Updating device_keys for device %r for user %s at %d",
            device_id,
            user_id,
            time_now,
        )
        log_kv(
            {
                "message": "Updating device_keys for user.",
                "user_id": user_id,
                "device_id": device_id,
            }
        )
        # TODO: Sign the JSON with the server key
        changed = await self.store.set_e2e_device_keys(
            user_id, device_id, time_now, device_keys
        )
        if changed:
            # Only notify about device updates *if* the keys actually changed
            await self.device_handler.notify_device_update(user_id, [device_id])

        # the device should have been registered already, but it may have been
        # deleted due to a race with a DELETE request. Or we may be using an
        # old access_token without an associated device_id. Either way, we
        # need to double-check the device is registered to avoid ending up with
        # keys without a corresponding device.
        await self.device_handler.check_device_registered(user_id, device_id)

    async def _upload_one_time_keys_for_user(
        self, user_id: str, device_id: str, time_now: int, one_time_keys: JsonDict
    ) -> None:
        # We take out a lock so that we don't have to worry about a client
        # sending duplicate requests.
        lock_key = f"{user_id}_{device_id}"
        async with self._worker_lock_handler.acquire_lock(
            ONE_TIME_KEY_UPLOAD, lock_key
        ):
            logger.info(
                "Adding one_time_keys %r for device %r for user %r at %d",
                one_time_keys.keys(),
                device_id,
                user_id,
                time_now,
            )

            # make a list of (alg, id, key) tuples
            key_list = []
            for key_id, key_obj in one_time_keys.items():
                algorithm, key_id = key_id.split(":")
                key_list.append((algorithm, key_id, key_obj))

            # First we check if we have already persisted any of the keys.
            existing_key_map = await self.store.get_e2e_one_time_keys(
                user_id, device_id, [k_id for _, k_id, _ in key_list]
            )

            new_keys = []  # Keys that we need to insert. (alg, id, json) tuples.
            for algorithm, key_id, key in key_list:
                ex_json = existing_key_map.get((algorithm, key_id), None)
                if ex_json:
                    if not _one_time_keys_match(ex_json, key):
                        raise SynapseError(
                            400,
                            (
                                "One time key %s:%s already exists. "
                                "Old key: %s; new key: %r"
                            )
                            % (algorithm, key_id, ex_json, key),
                        )
                else:
                    new_keys.append(
                        (algorithm, key_id, encode_canonical_json(key).decode("ascii"))
                    )

            log_kv({"message": "Inserting new one_time_keys.", "keys": new_keys})
            await self.store.add_e2e_one_time_keys(
                user_id, device_id, time_now, new_keys
            )

    async def upload_signing_keys_for_user(
        self, user_id: str, keys: JsonDict
    ) -> JsonDict:
        """Upload signing keys for cross-signing

        Args:
            user_id: the user uploading the keys
            keys: the signing keys
        """
        # This can only be called from the main process.
        assert isinstance(self.device_handler, DeviceHandler)

        # if a master key is uploaded, then check it.  Otherwise, load the
        # stored master key, to check signatures on other keys
        if "master_key" in keys:
            master_key = keys["master_key"]

            _check_cross_signing_key(master_key, user_id, "master")
        else:
            master_key = await self.store.get_e2e_cross_signing_key(user_id, "master")

        # if there is no master key, then we can't do anything, because all the
        # other cross-signing keys need to be signed by the master key
        if not master_key:
            raise SynapseError(400, "No master key available", Codes.MISSING_PARAM)

        try:
            master_key_id, master_verify_key = get_verify_key_from_cross_signing_key(
                master_key
            )
        except ValueError:
            if "master_key" in keys:
                # the invalid key came from the request
                raise SynapseError(400, "Invalid master key", Codes.INVALID_PARAM)
            else:
                # the invalid key came from the database
                logger.error("Invalid master key found for user %s", user_id)
                raise SynapseError(500, "Invalid master key")

        # for the other cross-signing keys, make sure that they have valid
        # signatures from the master key
        if "self_signing_key" in keys:
            self_signing_key = keys["self_signing_key"]

            _check_cross_signing_key(
                self_signing_key, user_id, "self_signing", master_verify_key
            )

        if "user_signing_key" in keys:
            user_signing_key = keys["user_signing_key"]

            _check_cross_signing_key(
                user_signing_key, user_id, "user_signing", master_verify_key
            )

        # if everything checks out, then store the keys and send notifications
        deviceids = []
        if "master_key" in keys:
            await self.store.set_e2e_cross_signing_key(user_id, "master", master_key)
            deviceids.append(master_verify_key.version)
        if "self_signing_key" in keys:
            await self.store.set_e2e_cross_signing_key(
                user_id, "self_signing", self_signing_key
            )
            try:
                deviceids.append(
                    get_verify_key_from_cross_signing_key(self_signing_key)[1].version
                )
            except ValueError:
                raise SynapseError(400, "Invalid self-signing key", Codes.INVALID_PARAM)
        if "user_signing_key" in keys:
            await self.store.set_e2e_cross_signing_key(
                user_id, "user_signing", user_signing_key
            )
            # the signature stream matches the semantics that we want for
            # user-signing key updates: only the user themselves is notified of
            # their own user-signing key updates
            await self.device_handler.notify_user_signature_update(user_id, [user_id])

        # master key and self-signing key updates match the semantics of device
        # list updates: all users who share an encrypted room are notified
        if len(deviceids):
            await self.device_handler.notify_device_update(user_id, deviceids)

        return {}

    async def upload_signatures_for_device_keys(
        self, user_id: str, signatures: JsonDict
    ) -> JsonDict:
        """Upload device signatures for cross-signing

        Args:
            user_id: the user uploading the signatures
            signatures: map of users to devices to signed keys. This is the submission
            from the user; an exception will be raised if it is malformed.
        Returns:
            The response to be sent back to the client.  The response will have
                a "failures" key, which will be a dict mapping users to devices
                to errors for the signatures that failed.
        Raises:
            SynapseError: if the signatures dict is not valid.
        """
        # This can only be called from the main process.
        assert isinstance(self.device_handler, DeviceHandler)

        failures = {}

        # signatures to be stored.  Each item will be a SignatureListItem
        signature_list = []

        # split between checking signatures for own user and signatures for
        # other users, since we verify them with different keys
        self_signatures = signatures.get(user_id, {})
        other_signatures = {k: v for k, v in signatures.items() if k != user_id}

        self_signature_list, self_failures = await self._process_self_signatures(
            user_id, self_signatures
        )
        signature_list.extend(self_signature_list)
        failures.update(self_failures)

        other_signature_list, other_failures = await self._process_other_signatures(
            user_id, other_signatures
        )
        signature_list.extend(other_signature_list)
        failures.update(other_failures)

        # store the signature, and send the appropriate notifications for sync
        logger.debug("upload signature failures: %r", failures)
        await self.store.store_e2e_cross_signing_signatures(user_id, signature_list)

        self_device_ids = [item.target_device_id for item in self_signature_list]
        if self_device_ids:
            await self.device_handler.notify_device_update(user_id, self_device_ids)
        signed_users = [item.target_user_id for item in other_signature_list]
        if signed_users:
            await self.device_handler.notify_user_signature_update(
                user_id, signed_users
            )

        return {"failures": failures}

    async def _process_self_signatures(
        self, user_id: str, signatures: JsonDict
    ) -> Tuple[List["SignatureListItem"], Dict[str, Dict[str, dict]]]:
        """Process uploaded signatures of the user's own keys.

        Signatures of the user's own keys from this API come in two forms:
        - signatures of the user's devices by the user's self-signing key,
        - signatures of the user's master key by the user's devices.

        Args:
            user_id: the user uploading the keys
            signatures (dict[string, dict]): map of devices to signed keys

        Returns:
            A tuple of a list of signatures to store, and a map of users to
            devices to failure reasons

        Raises:
            SynapseError: if the input is malformed
        """
        signature_list: List["SignatureListItem"] = []
        failures: Dict[str, Dict[str, JsonDict]] = {}
        if not signatures:
            return signature_list, failures

        if not isinstance(signatures, dict):
            raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)

        try:
            # get our self-signing key to verify the signatures
            (
                _,
                self_signing_key_id,
                self_signing_verify_key,
            ) = await self._get_e2e_cross_signing_verify_key(user_id, "self_signing")

            # get our master key, since we may have received a signature of it.
            # We need to fetch it here so that we know what its key ID is, so
            # that we can check if a signature that was sent is a signature of
            # the master key or of a device
            (
                master_key,
                _,
                master_verify_key,
            ) = await self._get_e2e_cross_signing_verify_key(user_id, "master")

            # fetch our stored devices.  This is used to 1. verify
            # signatures on the master key, and 2. to compare with what
            # was sent if the device was signed
            devices = await self.store.get_e2e_device_keys_for_cs_api([(user_id, None)])

            if user_id not in devices:
                raise NotFoundError("No device keys found")

            devices = devices[user_id]
        except SynapseError as e:
            failure = _exception_to_failure(e)
            failures[user_id] = {device: failure for device in signatures.keys()}
            return signature_list, failures

        for device_id, device in signatures.items():
            # make sure submitted data is in the right form
            if not isinstance(device, dict):
                raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)

            try:
                if "signatures" not in device or user_id not in device["signatures"]:
                    # no signature was sent
                    raise SynapseError(
                        400, "Invalid signature", Codes.INVALID_SIGNATURE
                    )

                if device_id == master_verify_key.version:
                    # The signature is of the master key. This needs to be
                    # handled differently from signatures of normal devices.
                    master_key_signature_list = self._check_master_key_signature(
                        user_id, device_id, device, master_key, devices
                    )
                    signature_list.extend(master_key_signature_list)
                    continue

                # at this point, we have a device that should be signed
                # by the self-signing key
                if self_signing_key_id not in device["signatures"][user_id]:
                    # no signature was sent
                    raise SynapseError(
                        400, "Invalid signature", Codes.INVALID_SIGNATURE
                    )

                try:
                    stored_device = devices[device_id]
                except KeyError:
                    raise NotFoundError("Unknown device")
                if self_signing_key_id in stored_device.get("signatures", {}).get(
                    user_id, {}
                ):
                    # we already have a signature on this device, so we
                    # can skip it, since it should be exactly the same
                    continue

                _check_device_signature(
                    user_id, self_signing_verify_key, device, stored_device
                )

                signature = device["signatures"][user_id][self_signing_key_id]
                signature_list.append(
                    SignatureListItem(
                        self_signing_key_id, user_id, device_id, signature
                    )
                )
            except SynapseError as e:
                failures.setdefault(user_id, {})[device_id] = _exception_to_failure(e)

        return signature_list, failures

    def _check_master_key_signature(
        self,
        user_id: str,
        master_key_id: str,
        signed_master_key: JsonDict,
        stored_master_key: JsonMapping,
        devices: Dict[str, Dict[str, JsonDict]],
    ) -> List["SignatureListItem"]:
        """Check signatures of a user's master key made by their devices.

        Args:
            user_id: the user whose master key is being checked
            master_key_id: the ID of the user's master key
            signed_master_key: the user's signed master key that was uploaded
            stored_master_key: our previously-stored copy of the user's master key
            devices: the user's devices

        Returns:
            A list of signatures to store

        Raises:
            SynapseError: if a signature is invalid
        """
        # for each device that signed the master key, check the signature.
        master_key_signature_list = []
        sigs = signed_master_key["signatures"]
        for signing_key_id, signature in sigs[user_id].items():
            _, signing_device_id = signing_key_id.split(":", 1)
            if (
                signing_device_id not in devices
                or signing_key_id not in devices[signing_device_id]["keys"]
            ):
                # signed by an unknown device, or the
                # device does not have the key
                raise SynapseError(400, "Invalid signature", Codes.INVALID_SIGNATURE)

            # get the key and check the signature
            pubkey = devices[signing_device_id]["keys"][signing_key_id]
            verify_key = decode_verify_key_bytes(signing_key_id, decode_base64(pubkey))
            _check_device_signature(
                user_id, verify_key, signed_master_key, stored_master_key
            )

            master_key_signature_list.append(
                SignatureListItem(signing_key_id, user_id, master_key_id, signature)
            )

        return master_key_signature_list

    async def _process_other_signatures(
        self, user_id: str, signatures: Dict[str, dict]
    ) -> Tuple[List["SignatureListItem"], Dict[str, Dict[str, dict]]]:
        """Process uploaded signatures of other users' keys.  These will be the
        target user's master keys, signed by the uploading user's user-signing
        key.

        Args:
            user_id: the user uploading the keys
            signatures: map of users to devices to signed keys

        Returns:
            A list of signatures to store, and a map of users to devices to failure
            reasons

        Raises:
            SynapseError: if the input is malformed
        """
        signature_list: List["SignatureListItem"] = []
        failures: Dict[str, Dict[str, JsonDict]] = {}
        if not signatures:
            return signature_list, failures

        try:
            # get our user-signing key to verify the signatures
            (
                user_signing_key,
                user_signing_key_id,
                user_signing_verify_key,
            ) = await self._get_e2e_cross_signing_verify_key(user_id, "user_signing")
        except SynapseError as e:
            failure = _exception_to_failure(e)
            for user, devicemap in signatures.items():
                failures[user] = {device_id: failure for device_id in devicemap.keys()}
            return signature_list, failures

        for target_user, devicemap in signatures.items():
            # make sure submitted data is in the right form
            if not isinstance(devicemap, dict):
                raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)
            for device in devicemap.values():
                if not isinstance(device, dict):
                    raise SynapseError(400, "Invalid parameter", Codes.INVALID_PARAM)

            device_id = None
            try:
                # get the target user's master key, to make sure it matches
                # what was sent
                (
                    master_key,
                    master_key_id,
                    _,
                ) = await self._get_e2e_cross_signing_verify_key(
                    target_user, "master", user_id
                )

                # make sure that the target user's master key is the one that
                # was signed (and no others)
                device_id = master_key_id.split(":", 1)[1]
                if device_id not in devicemap:
                    logger.debug(
                        "upload signature: could not find signature for device %s",
                        device_id,
                    )
                    # set device to None so that the failure gets
                    # marked on all the signatures
                    device_id = None
                    raise NotFoundError("Unknown device")
                key = devicemap[device_id]
                other_devices = [k for k in devicemap.keys() if k != device_id]
                if other_devices:
                    # other devices were signed -- mark those as failures
                    logger.debug("upload signature: too many devices specified")
                    failure = _exception_to_failure(NotFoundError("Unknown device"))
                    failures[target_user] = {
                        device: failure for device in other_devices
                    }

                if user_signing_key_id in master_key.get("signatures", {}).get(
                    user_id, {}
                ):
                    # we already have the signature, so we can skip it
                    continue

                _check_device_signature(
                    user_id, user_signing_verify_key, key, master_key
                )

                signature = key["signatures"][user_id][user_signing_key_id]
                signature_list.append(
                    SignatureListItem(
                        user_signing_key_id, target_user, device_id, signature
                    )
                )
            except SynapseError as e:
                failure = _exception_to_failure(e)
                if device_id is None:
                    failures[target_user] = {
                        device_id: failure for device_id in devicemap.keys()
                    }
                else:
                    failures.setdefault(target_user, {})[device_id] = failure

        return signature_list, failures

    async def _get_e2e_cross_signing_verify_key(
        self, user_id: str, key_type: str, from_user_id: Optional[str] = None
    ) -> Tuple[JsonMapping, str, VerifyKey]:
        """Fetch locally or remotely query for a cross-signing public key.

        First, attempt to fetch the cross-signing public key from storage.
        If that fails, query the keys from the homeserver they belong to
        and update our local copy.

        Args:
            user_id: the user whose key should be fetched
            key_type: the type of key to fetch
            from_user_id: the user that we are fetching the keys for.
                This affects what signatures are fetched.

        Returns:
            The raw key data, the key ID, and the signedjson verify key

        Raises:
            NotFoundError: if the key is not found
            SynapseError: if `user_id` is invalid
        """
        user = UserID.from_string(user_id)
        key = await self.store.get_e2e_cross_signing_key(
            user_id, key_type, from_user_id
        )

        if key:
            # We found a copy of this key in our database. Decode and return it
            key_id, verify_key = get_verify_key_from_cross_signing_key(key)
            return key, key_id, verify_key

        # If we couldn't find the key locally, and we're looking for keys of
        # another user then attempt to fetch the missing key from the remote
        # user's server.
        #
        # We may run into this in possible edge cases where a user tries to
        # cross-sign a remote user, but does not share any rooms with them yet.
        # Thus, we would not have their key list yet. We instead fetch the key,
        # store it and notify clients of new, associated device IDs.
        if self.is_mine(user) or key_type not in ["master", "self_signing"]:
            # Note that master and self_signing keys are the only cross-signing keys we
            # can request over federation
            raise NotFoundError("No %s key found for %s" % (key_type, user_id))

        cross_signing_keys = await self._retrieve_cross_signing_keys_for_remote_user(
            user, key_type
        )
        if cross_signing_keys is None:
            raise NotFoundError("No %s key found for %s" % (key_type, user_id))

        return cross_signing_keys

    async def _retrieve_cross_signing_keys_for_remote_user(
        self,
        user: UserID,
        desired_key_type: str,
    ) -> Optional[Tuple[JsonMapping, str, VerifyKey]]:
        """Queries cross-signing keys for a remote user and saves them to the database

        Only the key specified by `key_type` will be returned, while all retrieved keys
        will be saved regardless

        Args:
            user: The user to query remote keys for
            desired_key_type: The type of key to receive. One of "master", "self_signing"

        Returns:
            A tuple of the retrieved key content, the key's ID and the matching VerifyKey.
            If the key cannot be retrieved, all values in the tuple will instead be None.
        """
        # This can only be called from the main process.
        assert isinstance(self.device_handler, DeviceHandler)

        try:
            remote_result = await self.federation.query_user_devices(
                user.domain, user.to_string()
            )
        except Exception as e:
            logger.warning(
                "Unable to query %s for cross-signing keys of user %s: %s %s",
                user.domain,
                user.to_string(),
                type(e),
                e,
            )
            return None

        # Process each of the retrieved cross-signing keys
        desired_key_data = None
        retrieved_device_ids = []
        for key_type in ["master", "self_signing"]:
            key_content = remote_result.get(key_type + "_key")
            if not key_content:
                continue

            # Ensure these keys belong to the correct user
            if "user_id" not in key_content:
                logger.warning(
                    "Invalid %s key retrieved, missing user_id field: %s",
                    key_type,
                    key_content,
                )
                continue
            if user.to_string() != key_content["user_id"]:
                logger.warning(
                    "Found %s key of user %s when querying for keys of user %s",
                    key_type,
                    key_content["user_id"],
                    user.to_string(),
                )
                continue

            # Validate the key contents
            try:
                # verify_key is a VerifyKey from signedjson, which uses
                # .version to denote the portion of the key ID after the
                # algorithm and colon, which is the device ID
                key_id, verify_key = get_verify_key_from_cross_signing_key(key_content)
            except ValueError as e:
                logger.warning(
                    "Invalid %s key retrieved: %s - %s %s",
                    key_type,
                    key_content,
                    type(e),
                    e,
                )
                continue

            # Note down the device ID attached to this key
            retrieved_device_ids.append(verify_key.version)

            # If this is the desired key type, save it and its ID/VerifyKey
            if key_type == desired_key_type:
                desired_key_data = key_content, key_id, verify_key

            # At the same time, store this key in the db for subsequent queries
            await self.store.set_e2e_cross_signing_key(
                user.to_string(), key_type, key_content
            )

        # Notify clients that new devices for this user have been discovered
        if retrieved_device_ids:
            # XXX is this necessary?
            await self.device_handler.notify_device_update(
                user.to_string(), retrieved_device_ids
            )

        return desired_key_data

    async def check_cross_signing_setup(self, user_id: str) -> Tuple[bool, bool]:
        """Checks if the user has cross-signing set up

        Args:
            user_id: The user to check

        Returns: a 2-tuple of booleans
            - whether the user has cross-signing set up, and
            - whether the user's master cross-signing key may be replaced without UIA.
        """
        (
            exists,
            ts_replacable_without_uia_before,
        ) = await self.store.get_master_cross_signing_key_updatable_before(user_id)

        if ts_replacable_without_uia_before is None:
            return exists, False
        else:
            return exists, self.clock.time_msec() < ts_replacable_without_uia_before

    async def has_different_keys(self, user_id: str, body: JsonDict) -> bool:
        """
        Check if a key provided in `body` differs from the same key stored in the DB. Returns
        true on the first difference. If a key exists in `body` but does not exist in the DB,
        returns True. If `body` has no keys, this always returns False.
        Note by 'key' we mean Matrix key rather than JSON key.

        The purpose of this function is to detect whether or not we need to apply UIA checks.
        We must apply UIA checks if any key in the database is being overwritten. If a key is
        being inserted for the first time, or if the key exactly matches what is in the database,
        then no UIA check needs to be performed.

        Args:
            user_id: The user who sent the `body`.
            body: The JSON request body from POST /keys/device_signing/upload
        Returns:
            True if any key in `body` has a different value in the database.
        """
        # Ensure that each key provided in the request body exactly matches the one we have stored.
        # The first time we see the DB having a different key to the matching request key, bail.
        # Note: we do not care if the DB has a key which the request does not specify, as we only
        # care about *replacements* or *insertions* (i.e UPSERT)
        req_body_key_to_db_key = {
            "master_key": "master",
            "self_signing_key": "self_signing",
            "user_signing_key": "user_signing",
        }
        for req_body_key, db_key in req_body_key_to_db_key.items():
            if req_body_key in body:
                existing_key = await self.store.get_e2e_cross_signing_key(
                    user_id, db_key
                )
                if existing_key != body[req_body_key]:
                    return True
        return False


def _check_cross_signing_key(
    key: JsonDict, user_id: str, key_type: str, signing_key: Optional[VerifyKey] = None
) -> None:
    """Check a cross-signing key uploaded by a user.  Performs some basic sanity
    checking, and ensures that it is signed, if a signature is required.

    Args:
        key: the key data to verify
        user_id: the user whose key is being checked
        key_type: the type of key that the key should be
        signing_key: the signing key that the key should be signed with.  If
            omitted, signatures will not be checked.
    """
    if (
        key.get("user_id") != user_id
        or key_type not in key.get("usage", [])
        or len(key.get("keys", {})) != 1
    ):
        raise SynapseError(400, ("Invalid %s key" % (key_type,)), Codes.INVALID_PARAM)

    if signing_key:
        try:
            verify_signed_json(key, user_id, signing_key)
        except SignatureVerifyException:
            raise SynapseError(
                400, ("Invalid signature on %s key" % key_type), Codes.INVALID_SIGNATURE
            )


def _check_device_signature(
    user_id: str,
    verify_key: VerifyKey,
    signed_device: JsonDict,
    stored_device: JsonMapping,
) -> None:
    """Check that a signature on a device or cross-signing key is correct and
    matches the copy of the device/key that we have stored.  Throws an
    exception if an error is detected.

    Args:
        user_id: the user ID whose signature is being checked
        verify_key: the key to verify the device with
        signed_device: the uploaded signed device data
        stored_device: our previously stored copy of the device

    Raises:
        SynapseError: if the signature was invalid or the sent device is not the
            same as the stored device

    """

    # make sure that the device submitted matches what we have stored
    stripped_signed_device = {
        k: v for k, v in signed_device.items() if k not in ["signatures", "unsigned"]
    }
    stripped_stored_device = {
        k: v for k, v in stored_device.items() if k not in ["signatures", "unsigned"]
    }
    if stripped_signed_device != stripped_stored_device:
        logger.debug(
            "upload signatures: key does not match %s vs %s",
            signed_device,
            stored_device,
        )
        raise SynapseError(400, "Key does not match")

    try:
        verify_signed_json(signed_device, user_id, verify_key)
    except SignatureVerifyException:
        logger.debug("invalid signature on key")
        raise SynapseError(400, "Invalid signature", Codes.INVALID_SIGNATURE)


_NOT_READY_FOR_RETRY_FAILURE = {"status": 503, "message": "Not ready for retry"}


def _exception_to_failure(e: Exception) -> JsonDict:
    if isinstance(e, SynapseError):
        return {"status": e.code, "errcode": e.errcode, "message": str(e)}

    if isinstance(e, CodeMessageException):
        return {"status": e.code, "message": str(e)}

    if isinstance(e, NotRetryingDestination):
        return _NOT_READY_FOR_RETRY_FAILURE

    # include ConnectionRefused and other errors
    #
    # Note that some Exceptions (notably twisted's ResponseFailed etc) don't
    # give a string for e.message, which json then fails to serialize.
    return {"status": 503, "message": str(e)}


def _one_time_keys_match(old_key_json: str, new_key: JsonDict) -> bool:
    old_key = json_decoder.decode(old_key_json)

    # if either is a string rather than an object, they must match exactly
    if not isinstance(old_key, dict) or not isinstance(new_key, dict):
        return old_key == new_key

    # otherwise, we strip off the 'signatures' if any, because it's legitimate
    # for different upload attempts to have different signatures.
    old_key.pop("signatures", None)
    new_key_copy = dict(new_key)
    new_key_copy.pop("signatures", None)

    return old_key == new_key_copy


@attr.s(slots=True, auto_attribs=True)
class SignatureListItem:
    """An item in the signature list as used by upload_signatures_for_device_keys."""

    signing_key_id: str
    target_user_id: str
    target_device_id: str
    signature: JsonDict


class SigningKeyEduUpdater:
    """Handles incoming signing key updates from federation and updates the DB"""

    def __init__(self, hs: "HomeServer"):
        self.store = hs.get_datastores().main
        self.federation = hs.get_federation_client()
        self.clock = hs.get_clock()

        device_handler = hs.get_device_handler()
        assert isinstance(device_handler, DeviceHandler)
        self._device_handler = device_handler

        self._remote_edu_linearizer = Linearizer(name="remote_signing_key")

        # user_id -> list of updates waiting to be handled.
        self._pending_updates: Dict[str, List[Tuple[JsonDict, JsonDict]]] = {}

    async def incoming_signing_key_update(
        self, origin: str, edu_content: JsonDict
    ) -> None:
        """Called on incoming signing key update from federation. Responsible for
        parsing the EDU and adding to pending updates list.

        Args:
            origin: the server that sent the EDU
            edu_content: the contents of the EDU
        """

        user_id = edu_content.pop("user_id")
        master_key = edu_content.pop("master_key", None)
        self_signing_key = edu_content.pop("self_signing_key", None)

        if get_domain_from_id(user_id) != origin:
            logger.warning("Got signing key update edu for %r from %r", user_id, origin)
            return

        room_ids = await self.store.get_rooms_for_user(user_id)
        if not room_ids:
            # We don't share any rooms with this user. Ignore update, as we
            # probably won't get any further updates.
            return

        self._pending_updates.setdefault(user_id, []).append(
            (master_key, self_signing_key)
        )

        await self._handle_signing_key_updates(user_id)

    async def _handle_signing_key_updates(self, user_id: str) -> None:
        """Actually handle pending updates.

        Args:
            user_id: the user whose updates we are processing
        """

        async with self._remote_edu_linearizer.queue(user_id):
            pending_updates = self._pending_updates.pop(user_id, [])
            if not pending_updates:
                # This can happen since we batch updates
                return

            device_ids: List[str] = []

            logger.info("pending updates: %r", pending_updates)

            for master_key, self_signing_key in pending_updates:
                new_device_ids = await self._device_handler.device_list_updater.process_cross_signing_key_update(
                    user_id,
                    master_key,
                    self_signing_key,
                )
                device_ids = device_ids + new_device_ids

            await self._device_handler.notify_device_update(user_id, device_ids)
