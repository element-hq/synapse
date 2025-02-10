#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2021 The Matrix.org Foundation C.I.C.
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

import abc
import logging
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional, Tuple

import attr
from signedjson.key import (
    decode_verify_key_bytes,
    encode_verify_key_base64,
    get_verify_key,
    is_signing_algorithm_supported,
)
from signedjson.sign import SignatureVerifyException, signature_ids, verify_signed_json
from signedjson.types import VerifyKey
from unpaddedbase64 import decode_base64

from twisted.internet import defer

from synapse.api.errors import (
    Codes,
    HttpResponseException,
    RequestSendFailed,
    SynapseError,
)
from synapse.config.key import TrustedKeyServer
from synapse.events import EventBase
from synapse.events.utils import prune_event_dict
from synapse.logging.context import make_deferred_yieldable, run_in_background
from synapse.storage.keys import FetchKeyResult
from synapse.types import JsonDict
from synapse.util import unwrapFirstError
from synapse.util.async_helpers import yieldable_gather_results
from synapse.util.batching_queue import BatchingQueue
from synapse.util.retryutils import NotRetryingDestination

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True, cmp=False, auto_attribs=True)
class VerifyJsonRequest:
    """
    A request to verify a JSON object.

    Attributes:
        server_name: The name of the server to verify against.

        get_json_object: A callback to fetch the JSON object to verify.
            A callback is used to allow deferring the creation of the JSON
            object to verify until needed, e.g. for events we can defer
            creating the redacted copy. This reduces the memory usage when
            there are large numbers of in flight requests.

        minimum_valid_until_ts: time at which we require the signing key to
            be valid. (0 implies we don't care)

        key_ids: The set of key_ids to that could be used to verify the JSON object
    """

    server_name: str
    get_json_object: Callable[[], JsonDict]
    minimum_valid_until_ts: int
    key_ids: List[str]

    @staticmethod
    def from_json_object(
        server_name: str,
        json_object: JsonDict,
        minimum_valid_until_ms: int,
    ) -> "VerifyJsonRequest":
        """Create a VerifyJsonRequest to verify all signatures on a signed JSON
        object for the given server.
        """
        key_ids = signature_ids(json_object, server_name)
        return VerifyJsonRequest(
            server_name,
            lambda: json_object,
            minimum_valid_until_ms,
            key_ids=key_ids,
        )

    @staticmethod
    def from_event(
        server_name: str,
        event: EventBase,
        minimum_valid_until_ms: int,
    ) -> "VerifyJsonRequest":
        """Create a VerifyJsonRequest to verify all signatures on an event
        object for the given server.
        """
        key_ids = list(event.signatures.get(server_name, []))
        return VerifyJsonRequest(
            server_name,
            # We defer creating the redacted json object, as it uses a lot more
            # memory than the Event object itself.
            lambda: prune_event_dict(event.room_version, event.get_pdu_json()),
            minimum_valid_until_ms,
            key_ids=key_ids,
        )


class KeyLookupError(ValueError):
    pass


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _FetchKeyRequest:
    """A request for keys for a given server.

    We will continue to try and fetch until we have all the keys listed under
    `key_ids` (with an appropriate `valid_until_ts` property) or we run out of
    places to fetch keys from.

    Attributes:
        server_name: The name of the server that owns the keys.
        minimum_valid_until_ts: The timestamp which the keys must be valid until.
        key_ids: The IDs of the keys to attempt to fetch
    """

    server_name: str
    minimum_valid_until_ts: int
    key_ids: List[str]


class Keyring:
    """Handles verifying signed JSON objects and fetching the keys needed to do
    so.
    """

    def __init__(
        self, hs: "HomeServer", key_fetchers: "Optional[Iterable[KeyFetcher]]" = None
    ):
        if key_fetchers is None:
            # Always fetch keys from the database.
            mutable_key_fetchers: List[KeyFetcher] = [StoreKeyFetcher(hs)]
            # Fetch keys from configured trusted key servers, if any exist.
            key_servers = hs.config.key.key_servers
            if key_servers:
                mutable_key_fetchers.append(PerspectivesKeyFetcher(hs))
            # Finally, fetch keys from the origin server directly.
            mutable_key_fetchers.append(ServerKeyFetcher(hs))

            self._key_fetchers: Iterable[KeyFetcher] = tuple(mutable_key_fetchers)
        else:
            self._key_fetchers = key_fetchers

        self._fetch_keys_queue: BatchingQueue[
            _FetchKeyRequest, Dict[str, Dict[str, FetchKeyResult]]
        ] = BatchingQueue(
            "keyring_server",
            clock=hs.get_clock(),
            # The method called to fetch each key
            process_batch_callback=self._inner_fetch_key_requests,
        )

        self._is_mine_server_name = hs.is_mine_server_name

        # build a FetchKeyResult for each of our own keys, to shortcircuit the
        # fetcher.
        self._local_verify_keys: Dict[str, FetchKeyResult] = {}
        for key_id, key in hs.config.key.old_signing_keys.items():
            self._local_verify_keys[key_id] = FetchKeyResult(
                verify_key=key, valid_until_ts=key.expired
            )

        vk = get_verify_key(hs.signing_key)
        self._local_verify_keys[f"{vk.alg}:{vk.version}"] = FetchKeyResult(
            verify_key=vk,
            valid_until_ts=2**63,  # fake future timestamp
        )

    async def verify_json_for_server(
        self,
        server_name: str,
        json_object: JsonDict,
        validity_time: int,
    ) -> None:
        """Verify that a JSON object has been signed by a given server

        Completes if the the object was correctly signed, otherwise raises.

        Args:
            server_name: name of the server which must have signed this object

            json_object: object to be checked

            validity_time: timestamp at which we require the signing key to
                be valid. (0 implies we don't care)
        """

        request = VerifyJsonRequest.from_json_object(
            server_name,
            json_object,
            validity_time,
        )
        return await self.process_request(request)

    def verify_json_objects_for_server(
        self, server_and_json: Iterable[Tuple[str, dict, int]]
    ) -> List["defer.Deferred[None]"]:
        """Bulk verifies signatures of json objects, bulk fetching keys as
        necessary.

        Args:
            server_and_json:
                Iterable of (server_name, json_object, validity_time)
                tuples.

                validity_time is a timestamp at which the signing key must be
                valid.

        Returns:
            For each input triplet, a deferred indicating success or failure to
            verify each json object's signature for the given server_name. The
            deferreds run their callbacks in the sentinel logcontext.
        """
        return [
            run_in_background(
                self.process_request,
                VerifyJsonRequest.from_json_object(
                    server_name,
                    json_object,
                    validity_time,
                ),
            )
            for server_name, json_object, validity_time in server_and_json
        ]

    async def verify_event_for_server(
        self,
        server_name: str,
        event: EventBase,
        validity_time: int,
    ) -> None:
        await self.process_request(
            VerifyJsonRequest.from_event(
                server_name,
                event,
                validity_time,
            )
        )

    async def process_request(self, verify_request: VerifyJsonRequest) -> None:
        """Processes the `VerifyJsonRequest`. Raises if the object is not signed
        by the server, the signatures don't match or we failed to fetch the
        necessary keys.
        """

        if not verify_request.key_ids:
            raise SynapseError(
                400,
                f"Not signed by {verify_request.server_name}",
                Codes.UNAUTHORIZED,
            )

        found_keys: Dict[str, FetchKeyResult] = {}

        # If we are the originating server, short-circuit the key-fetch for any keys
        # we already have
        if self._is_mine_server_name(verify_request.server_name):
            for key_id in verify_request.key_ids:
                if key_id in self._local_verify_keys:
                    found_keys[key_id] = self._local_verify_keys[key_id]

        key_ids_to_find = set(verify_request.key_ids) - found_keys.keys()
        if key_ids_to_find:
            # Add the keys we need to verify to the queue for retrieval. We queue
            # up requests for the same server so we don't end up with many in flight
            # requests for the same keys.
            key_request = _FetchKeyRequest(
                server_name=verify_request.server_name,
                minimum_valid_until_ts=verify_request.minimum_valid_until_ts,
                key_ids=list(key_ids_to_find),
            )
            found_keys_by_server = await self._fetch_keys_queue.add_to_queue(
                key_request, key=verify_request.server_name
            )

            # Since we batch up requests the returned set of keys may contain keys
            # from other servers, so we pull out only the ones we care about.
            found_keys.update(found_keys_by_server.get(verify_request.server_name, {}))

        # Verify each signature we got valid keys for, raising if we can't
        # verify any of them.
        verified = False
        for key_id in verify_request.key_ids:
            key_result = found_keys.get(key_id)
            if not key_result:
                continue

            if key_result.valid_until_ts < verify_request.minimum_valid_until_ts:
                continue

            await self._process_json(key_result.verify_key, verify_request)
            verified = True

        if not verified:
            raise SynapseError(
                401,
                f"Failed to find any key to satisfy: {key_request}",
                Codes.UNAUTHORIZED,
            )

    async def _process_json(
        self, verify_key: VerifyKey, verify_request: VerifyJsonRequest
    ) -> None:
        """Processes the `VerifyJsonRequest`. Raises if the signature can't be
        verified.
        """
        try:
            verify_signed_json(
                verify_request.get_json_object(),
                verify_request.server_name,
                verify_key,
            )
        except SignatureVerifyException as e:
            logger.debug(
                "Error verifying signature for %s:%s:%s with key %s: %s",
                verify_request.server_name,
                verify_key.alg,
                verify_key.version,
                encode_verify_key_base64(verify_key),
                str(e),
            )
            raise SynapseError(
                401,
                "Invalid signature for server %s with key %s:%s: %s"
                % (
                    verify_request.server_name,
                    verify_key.alg,
                    verify_key.version,
                    str(e),
                ),
                Codes.UNAUTHORIZED,
            )

    async def _inner_fetch_key_requests(
        self, requests: List[_FetchKeyRequest]
    ) -> Dict[str, Dict[str, FetchKeyResult]]:
        """Processing function for the queue of `_FetchKeyRequest`.

        Takes a list of key fetch requests, de-duplicates them and then carries out
        each request by invoking self._inner_fetch_key_request.

        Args:
            requests: A list of requests for homeserver verify keys.

        Returns:
            {server name: {key id: fetch key result}}
        """

        logger.debug("Starting fetch for %s", requests)

        # First we need to deduplicate requests for the same key. We do this by
        # taking the *maximum* requested `minimum_valid_until_ts` for each pair
        # of server name/key ID.
        server_to_key_to_ts: Dict[str, Dict[str, int]] = {}
        for request in requests:
            by_server = server_to_key_to_ts.setdefault(request.server_name, {})
            for key_id in request.key_ids:
                existing_ts = by_server.get(key_id, 0)
                by_server[key_id] = max(request.minimum_valid_until_ts, existing_ts)

        deduped_requests = [
            _FetchKeyRequest(server_name, minimum_valid_ts, [key_id])
            for server_name, by_server in server_to_key_to_ts.items()
            for key_id, minimum_valid_ts in by_server.items()
        ]

        logger.debug("Deduplicated key requests to %s", deduped_requests)

        # For each key we call `_inner_verify_request` which will handle
        # fetching each key. Note these shouldn't throw if we fail to contact
        # other servers etc.
        results_per_request = await yieldable_gather_results(
            self._inner_fetch_key_request,
            deduped_requests,
        )

        # We now convert the returned list of results into a map from server
        # name to key ID to FetchKeyResult, to return.
        to_return: Dict[str, Dict[str, FetchKeyResult]] = {}
        for request, results in zip(deduped_requests, results_per_request):
            to_return_by_server = to_return.setdefault(request.server_name, {})
            for key_id, key_result in results.items():
                existing = to_return_by_server.get(key_id)
                if not existing or existing.valid_until_ts < key_result.valid_until_ts:
                    to_return_by_server[key_id] = key_result

        return to_return

    async def _inner_fetch_key_request(
        self, verify_request: _FetchKeyRequest
    ) -> Dict[str, FetchKeyResult]:
        """Attempt to fetch the given key by calling each key fetcher one by one.

        If a key is found, check whether its `valid_until_ts` attribute satisfies the
        `minimum_valid_until_ts` attribute of the `verify_request`. If it does, we
        refrain from asking subsequent fetchers for that key.

        Even if the above check fails, we still return the found key - the caller may
        still find the invalid key result useful. In this case, we continue to ask
        subsequent fetchers for the invalid key, in case they return a valid result
        for it. This can happen when fetching a stale key result from the database,
        before querying the origin server for an up-to-date result.

        Args:
            verify_request: The request for a verify key. Can include multiple key IDs.

        Returns:
            A map of {key_id: the key fetch result}.
        """
        logger.debug("Starting fetch for %s", verify_request)

        found_keys: Dict[str, FetchKeyResult] = {}
        missing_key_ids = set(verify_request.key_ids)

        for fetcher in self._key_fetchers:
            if not missing_key_ids:
                break

            logger.debug("Getting keys from %s for %s", fetcher, verify_request)
            keys = await fetcher.get_keys(
                verify_request.server_name,
                list(missing_key_ids),
                verify_request.minimum_valid_until_ts,
            )

            for key_id, key in keys.items():
                if not key:
                    continue

                # If we already have a result for the given key ID, we keep the
                # one with the highest `valid_until_ts`.
                existing_key = found_keys.get(key_id)
                if existing_key and existing_key.valid_until_ts > key.valid_until_ts:
                    continue

                # Check if this key's expiry timestamp is valid for the verify request.
                if key.valid_until_ts >= verify_request.minimum_valid_until_ts:
                    # Stop looking for this key from subsequent fetchers.
                    missing_key_ids.discard(key_id)

                # We always store the returned key even if it doesn't meet the
                # `minimum_valid_until_ts` requirement, as some verification
                # requests may still be able to be satisfied by it.
                found_keys[key_id] = key

        return found_keys


class KeyFetcher(metaclass=abc.ABCMeta):
    def __init__(self, hs: "HomeServer"):
        self._queue = BatchingQueue(
            self.__class__.__name__, hs.get_clock(), self._fetch_keys
        )

    async def get_keys(
        self, server_name: str, key_ids: List[str], minimum_valid_until_ts: int
    ) -> Dict[str, FetchKeyResult]:
        results = await self._queue.add_to_queue(
            _FetchKeyRequest(
                server_name=server_name,
                key_ids=key_ids,
                minimum_valid_until_ts=minimum_valid_until_ts,
            )
        )
        return results.get(server_name, {})

    @abc.abstractmethod
    async def _fetch_keys(
        self, keys_to_fetch: List[_FetchKeyRequest]
    ) -> Dict[str, Dict[str, FetchKeyResult]]:
        pass


class StoreKeyFetcher(KeyFetcher):
    """KeyFetcher impl which fetches keys from our data store"""

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.store = hs.get_datastores().main

    async def _fetch_keys(
        self, keys_to_fetch: List[_FetchKeyRequest]
    ) -> Dict[str, Dict[str, FetchKeyResult]]:
        key_ids_to_fetch = (
            (queue_value.server_name, key_id)
            for queue_value in keys_to_fetch
            for key_id in queue_value.key_ids
        )

        res = await self.store.get_server_keys_json(key_ids_to_fetch)
        keys: Dict[str, Dict[str, FetchKeyResult]] = {}
        for (server_name, key_id), key in res.items():
            keys.setdefault(server_name, {})[key_id] = key
        return keys


class BaseV2KeyFetcher(KeyFetcher):
    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)

        self.store = hs.get_datastores().main

    async def process_v2_response(
        self, from_server: str, response_json: JsonDict, time_added_ms: int
    ) -> Dict[str, FetchKeyResult]:
        """Parse a 'Server Keys' structure from the result of a /key request

        This is used to parse either the entirety of the response from
        GET /_matrix/key/v2/server, or a single entry from the list returned by
        POST /_matrix/key/v2/query.

        Checks that each signature in the response that claims to come from the origin
        server is valid, and that there is at least one such signature.

        Stores the json in server_keys_json so that it can be used for future responses
        to /_matrix/key/v2/query.

        Args:
            from_server: the name of the server producing this result: either
                the origin server for a /_matrix/key/v2/server request, or the notary
                for a /_matrix/key/v2/query.

            response_json: the json-decoded Server Keys response object

            time_added_ms: the timestamp to record in server_keys_json

        Returns:
            Map from key_id to result object
        """
        ts_valid_until_ms = response_json["valid_until_ts"]

        # start by extracting the keys from the response, since they may be required
        # to validate the signature on the response.
        verify_keys = {}
        for key_id, key_data in response_json["verify_keys"].items():
            if is_signing_algorithm_supported(key_id):
                key_base64 = key_data["key"]
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_keys[key_id] = FetchKeyResult(
                    verify_key=verify_key, valid_until_ts=ts_valid_until_ms
                )

        server_name = response_json["server_name"]
        verified = False
        for key_id in response_json["signatures"].get(server_name, {}):
            key = verify_keys.get(key_id)
            if not key:
                # the key may not be present in verify_keys if:
                #  * we got the key from the notary server, and:
                #  * the key belongs to the notary server, and:
                #  * the notary server is using a different key to sign notary
                #    responses.
                continue

            verify_signed_json(response_json, server_name, key.verify_key)
            verified = True
            break

        if not verified:
            raise KeyLookupError(
                "Key response for %s is not signed by the origin server"
                % (server_name,)
            )

        for key_id, key_data in response_json.get("old_verify_keys", {}).items():
            if is_signing_algorithm_supported(key_id):
                key_base64 = key_data["key"]
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_keys[key_id] = FetchKeyResult(
                    verify_key=verify_key, valid_until_ts=key_data["expired_ts"]
                )

        await self.store.store_server_keys_response(
            server_name=server_name,
            from_server=from_server,
            ts_added_ms=time_added_ms,
            verify_keys=verify_keys,
            response_json=response_json,
        )

        return verify_keys


class PerspectivesKeyFetcher(BaseV2KeyFetcher):
    """KeyFetcher impl which fetches keys from the "perspectives" servers"""

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.clock = hs.get_clock()
        self.client = hs.get_federation_http_client()
        self.key_servers = hs.config.key.key_servers

    async def _fetch_keys(
        self, keys_to_fetch: List[_FetchKeyRequest]
    ) -> Dict[str, Dict[str, FetchKeyResult]]:
        """see KeyFetcher._fetch_keys"""

        async def get_key(key_server: TrustedKeyServer) -> Dict:
            try:
                return await self.get_server_verify_key_v2_indirect(
                    keys_to_fetch, key_server
                )
            except KeyLookupError as e:
                logger.warning(
                    "Key lookup failed from %r: %s", key_server.server_name, e
                )
            except Exception as e:
                logger.exception(
                    "Unable to get key from %r: %s %s",
                    key_server.server_name,
                    type(e).__name__,
                    str(e),
                )

            return {}

        results = await make_deferred_yieldable(
            defer.gatherResults(
                [run_in_background(get_key, server) for server in self.key_servers],
                consumeErrors=True,
            ).addErrback(unwrapFirstError)
        )

        union_of_keys: Dict[str, Dict[str, FetchKeyResult]] = {}
        for result in results:
            for server_name, keys in result.items():
                union_of_keys.setdefault(server_name, {}).update(keys)

        return union_of_keys

    async def get_server_verify_key_v2_indirect(
        self, keys_to_fetch: List[_FetchKeyRequest], key_server: TrustedKeyServer
    ) -> Dict[str, Dict[str, FetchKeyResult]]:
        """
        Args:
            keys_to_fetch:
                the keys to be fetched.

            key_server: notary server to query for the keys

        Returns:
            Map from server_name -> key_id -> FetchKeyResult

        Raises:
            KeyLookupError if there was an error processing the entire response from
                the server
        """
        perspective_name = key_server.server_name
        logger.info(
            "Requesting keys %s from notary server %s",
            keys_to_fetch,
            perspective_name,
        )

        request: JsonDict = {}
        for queue_value in keys_to_fetch:
            # there may be multiple requests for each server, so we have to merge
            # them intelligently.
            request_for_server = {
                key_id: {
                    "minimum_valid_until_ts": queue_value.minimum_valid_until_ts,
                }
                for key_id in queue_value.key_ids
            }
            request.setdefault(queue_value.server_name, {}).update(request_for_server)

        logger.debug("Request to notary server %s: %s", perspective_name, request)

        try:
            query_response = await self.client.post_json(
                destination=perspective_name,
                path="/_matrix/key/v2/query",
                data={"server_keys": request},
            )
        except (NotRetryingDestination, RequestSendFailed) as e:
            # these both have str() representations which we can't really improve upon
            raise KeyLookupError(str(e))
        except HttpResponseException as e:
            raise KeyLookupError("Remote server returned an error: %s" % (e,))

        logger.debug(
            "Response from notary server %s: %s", perspective_name, query_response
        )

        keys: Dict[str, Dict[str, FetchKeyResult]] = {}
        added_keys: Dict[Tuple[str, str], FetchKeyResult] = {}

        time_now_ms = self.clock.time_msec()

        assert isinstance(query_response, dict)
        for response in query_response["server_keys"]:
            # do this first, so that we can give useful errors thereafter
            server_name = response.get("server_name")
            if not isinstance(server_name, str):
                raise KeyLookupError(
                    "Malformed response from key notary server %s: invalid server_name"
                    % (perspective_name,)
                )

            try:
                self._validate_perspectives_response(key_server, response)

                processed_response = await self.process_v2_response(
                    perspective_name, response, time_added_ms=time_now_ms
                )
            except KeyLookupError as e:
                logger.warning(
                    "Error processing response from key notary server %s for origin "
                    "server %s: %s",
                    perspective_name,
                    server_name,
                    e,
                )
                # we continue to process the rest of the response
                continue

            for key_id, key in processed_response.items():
                dict_key = (server_name, key_id)
                if dict_key in added_keys:
                    already_present_key = added_keys[dict_key]
                    logger.warning(
                        "Duplicate server keys for %s (%s) from perspective %s (%r, %r)",
                        server_name,
                        key_id,
                        perspective_name,
                        already_present_key,
                        key,
                    )

                    if already_present_key.valid_until_ts > key.valid_until_ts:
                        # Favour the entry with the largest valid_until_ts,
                        # as `old_verify_keys` are also collected from this
                        # response.
                        continue

                added_keys[dict_key] = key

            keys.setdefault(server_name, {}).update(processed_response)

        return keys

    def _validate_perspectives_response(
        self, key_server: TrustedKeyServer, response: JsonDict
    ) -> None:
        """Optionally check the signature on the result of a /key/query request

        Args:
            key_server: the notary server that produced this result

            response: the json-decoded Server Keys response object
        """
        perspective_name = key_server.server_name
        perspective_keys = key_server.verify_keys

        if perspective_keys is None:
            # signature checking is disabled on this server
            return

        if (
            "signatures" not in response
            or perspective_name not in response["signatures"]
        ):
            raise KeyLookupError("Response not signed by the notary server")

        verified = False
        for key_id in response["signatures"][perspective_name]:
            if key_id in perspective_keys:
                verify_signed_json(response, perspective_name, perspective_keys[key_id])
                verified = True

        if not verified:
            raise KeyLookupError(
                "Response not signed with a known key: signed with: %r, known keys: %r"
                % (
                    list(response["signatures"][perspective_name].keys()),
                    list(perspective_keys.keys()),
                )
            )


class ServerKeyFetcher(BaseV2KeyFetcher):
    """KeyFetcher impl which fetches keys from the origin servers"""

    def __init__(self, hs: "HomeServer"):
        super().__init__(hs)
        self.clock = hs.get_clock()
        self.client = hs.get_federation_http_client()

    async def get_keys(
        self, server_name: str, key_ids: List[str], minimum_valid_until_ts: int
    ) -> Dict[str, FetchKeyResult]:
        results = await self._queue.add_to_queue(
            _FetchKeyRequest(
                server_name=server_name,
                key_ids=key_ids,
                minimum_valid_until_ts=minimum_valid_until_ts,
            ),
            key=server_name,
        )
        return results.get(server_name, {})

    async def _fetch_keys(
        self, keys_to_fetch: List[_FetchKeyRequest]
    ) -> Dict[str, Dict[str, FetchKeyResult]]:
        """
        Args:
            keys_to_fetch:
                the keys to be fetched. server_name -> key_ids

        Returns:
            Map from server_name -> key_id -> FetchKeyResult
        """

        # We only need to do one request per server.
        servers_to_fetch = {k.server_name for k in keys_to_fetch}

        results = {}

        async def get_keys(server_name: str) -> None:
            try:
                keys = await self.get_server_verify_keys_v2_direct(server_name)
                results[server_name] = keys
            except KeyLookupError as e:
                logger.warning("Error looking up keys from %s: %s", server_name, e)
            except Exception:
                logger.exception("Error getting keys from %s", server_name)

        await yieldable_gather_results(get_keys, servers_to_fetch)
        return results

    async def get_server_verify_keys_v2_direct(
        self, server_name: str
    ) -> Dict[str, FetchKeyResult]:
        """

        Args:
            server_name: Server to request keys from

        Returns:
            Map from key ID to lookup result

        Raises:
            KeyLookupError if there was a problem making the lookup
        """
        time_now_ms = self.clock.time_msec()
        try:
            response = await self.client.get_json(
                destination=server_name,
                path="/_matrix/key/v2/server",
                ignore_backoff=True,
                # we only give the remote server 10s to respond. It should be an
                # easy request to handle, so if it doesn't reply within 10s, it's
                # probably not going to.
                #
                # Furthermore, when we are acting as a notary server, we cannot
                # wait all day for all of the origin servers, as the requesting
                # server will otherwise time out before we can respond.
                #
                # (Note that get_json may make 4 attempts, so this can still take
                # almost 45 seconds to fetch the headers, plus up to another 60s to
                # read the response).
                timeout=10000,
            )
        except (NotRetryingDestination, RequestSendFailed) as e:
            # these both have str() representations which we can't really improve
            # upon
            raise KeyLookupError(str(e))
        except HttpResponseException as e:
            raise KeyLookupError("Remote server returned an error: %s" % (e,))

        assert isinstance(response, dict)
        if response["server_name"] != server_name:
            raise KeyLookupError(
                "Expected a response for server %r not %r"
                % (server_name, response["server_name"])
            )

        return await self.process_v2_response(
            from_server=server_name,
            response_json=response,
            time_added_ms=time_now_ms,
        )
