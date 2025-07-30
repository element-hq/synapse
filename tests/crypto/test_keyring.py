#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2017-2021 The Matrix.org Foundation C.I.C
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
import time
from typing import Any, Dict, List, Optional, cast
from unittest.mock import Mock

import attr
import canonicaljson
import signedjson.key
import signedjson.sign
from signedjson.key import encode_verify_key_base64, get_verify_key
from signedjson.types import SigningKey, VerifyKey

from twisted.internet import defer
from twisted.internet.defer import Deferred, ensureDeferred
from twisted.internet.testing import MemoryReactor

from synapse.api.errors import SynapseError
from synapse.crypto import keyring
from synapse.crypto.keyring import (
    PerspectivesKeyFetcher,
    ServerKeyFetcher,
    StoreKeyFetcher,
)
from synapse.logging.context import (
    ContextRequest,
    LoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.server import HomeServer
from synapse.storage.keys import FetchKeyResult
from synapse.types import JsonDict
from synapse.util import Clock

from tests import unittest
from tests.unittest import logcontext_clean, override_config


class MockPerspectiveServer:
    def __init__(self) -> None:
        self.server_name = "mock_server"
        self.key = signedjson.key.generate_signing_key("0")

    def get_verify_keys(self) -> Dict[str, str]:
        vk = signedjson.key.get_verify_key(self.key)
        return {"%s:%s" % (vk.alg, vk.version): encode_verify_key_base64(vk)}

    def get_signed_key(self, server_name: str, verify_key: VerifyKey) -> JsonDict:
        key_id = "%s:%s" % (verify_key.alg, verify_key.version)
        res = {
            "server_name": server_name,
            "old_verify_keys": {},
            "valid_until_ts": time.time() * 1000 + 3600,
            "verify_keys": {key_id: {"key": encode_verify_key_base64(verify_key)}},
        }
        self.sign_response(res)
        return res

    def sign_response(self, res: JsonDict) -> None:
        signedjson.sign.sign_json(res, self.server_name, self.key)


@attr.s(slots=True, auto_attribs=True)
class FakeRequest:
    id: str


@logcontext_clean
class KeyringTestCase(unittest.HomeserverTestCase):
    def check_context(
        self, val: ContextRequest, expected: Optional[ContextRequest]
    ) -> ContextRequest:
        self.assertEqual(getattr(current_context(), "request", None), expected)
        return val

    def test_verify_json_objects_for_server_awaits_previous_requests(self) -> None:
        mock_fetcher = Mock()
        mock_fetcher.get_keys = Mock()
        kr = keyring.Keyring(self.hs, key_fetchers=(mock_fetcher,))

        # a signed object that we are going to try to validate
        key1 = signedjson.key.generate_signing_key("1")
        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, "server10", key1)

        # start off a first set of lookups. We make the mock fetcher block until this
        # deferred completes.
        first_lookup_deferred: "Deferred[None]" = Deferred()

        async def first_lookup_fetch(
            server_name: str, key_ids: List[str], minimum_valid_until_ts: int
        ) -> Dict[str, FetchKeyResult]:
            # self.assertEqual(current_context().request.id, "context_11")
            self.assertEqual(server_name, "server10")
            self.assertEqual(key_ids, [get_key_id(key1)])
            self.assertEqual(minimum_valid_until_ts, 0)

            await make_deferred_yieldable(first_lookup_deferred)
            return {get_key_id(key1): FetchKeyResult(get_verify_key(key1), 100)}

        mock_fetcher.get_keys.side_effect = first_lookup_fetch

        async def first_lookup() -> None:
            with LoggingContext(
                "context_11", request=cast(ContextRequest, FakeRequest("context_11"))
            ):
                res_deferreds = kr.verify_json_objects_for_server(
                    [("server10", json1, 0), ("server11", {}, 0)]
                )

                # the unsigned json should be rejected pretty quickly
                self.assertTrue(res_deferreds[1].called)
                try:
                    await res_deferreds[1]
                    self.assertFalse("unsigned json didn't cause a failure")
                except SynapseError:
                    pass

                self.assertFalse(res_deferreds[0].called)
                res_deferreds[0].addBoth(self.check_context, None)

                await make_deferred_yieldable(res_deferreds[0])

        d0 = ensureDeferred(first_lookup())

        self.pump()

        mock_fetcher.get_keys.assert_called_once()

        # a second request for a server with outstanding requests
        # should block rather than start a second call

        async def second_lookup_fetch(
            server_name: str, key_ids: List[str], minimum_valid_until_ts: int
        ) -> Dict[str, FetchKeyResult]:
            # self.assertEqual(current_context().request.id, "context_12")
            return {get_key_id(key1): FetchKeyResult(get_verify_key(key1), 100)}

        mock_fetcher.get_keys.reset_mock()
        mock_fetcher.get_keys.side_effect = second_lookup_fetch
        second_lookup_state = [0]

        async def second_lookup() -> None:
            with LoggingContext(
                "context_12", request=cast(ContextRequest, FakeRequest("context_12"))
            ):
                res_deferreds_2 = kr.verify_json_objects_for_server(
                    [
                        (
                            "server10",
                            json1,
                            0,
                        )
                    ]
                )
                res_deferreds_2[0].addBoth(self.check_context, None)
                second_lookup_state[0] = 1
                await make_deferred_yieldable(res_deferreds_2[0])
                second_lookup_state[0] = 2

        d2 = ensureDeferred(second_lookup())

        self.pump()
        # the second request should be pending, but the fetcher should not yet have been
        # called
        self.assertEqual(second_lookup_state[0], 1)
        mock_fetcher.get_keys.assert_not_called()

        # complete the first request
        first_lookup_deferred.callback(None)

        # and now both verifications should succeed.
        self.get_success(d0)
        self.get_success(d2)

    def test_verify_json_for_server(self) -> None:
        kr = keyring.Keyring(self.hs)

        key1 = signedjson.key.generate_signing_key("1")
        r = self.hs.get_datastores().main.store_server_keys_response(
            "server9",
            from_server="test",
            ts_added_ms=int(time.time() * 1000),
            verify_keys={
                get_key_id(key1): FetchKeyResult(
                    verify_key=get_verify_key(key1), valid_until_ts=1000
                )
            },
            # The entire response gets signed & stored, just include the bits we
            # care about.
            response_json={
                "verify_keys": {
                    get_key_id(key1): {
                        "key": encode_verify_key_base64(get_verify_key(key1))
                    }
                }
            },
        )
        self.get_success(r)

        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, "server9", key1)

        # should fail immediately on an unsigned object
        d = kr.verify_json_for_server("server9", {}, 0)
        self.get_failure(d, SynapseError)

        # should succeed on a signed object
        d = kr.verify_json_for_server("server9", json1, 500)
        # self.assertFalse(d.called)
        self.get_success(d)

    def test_verify_for_local_server(self) -> None:
        """Ensure that locally signed JSON can be verified without fetching keys
        over federation
        """
        kr = keyring.Keyring(self.hs)
        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, self.hs.hostname, self.hs.signing_key)

        # Test that verify_json_for_server succeeds on a object signed by ourselves
        d = kr.verify_json_for_server(self.hs.hostname, json1, 0)
        self.get_success(d)

    OLD_KEY = signedjson.key.generate_signing_key("old")

    @override_config(
        {
            "old_signing_keys": {
                f"{OLD_KEY.alg}:{OLD_KEY.version}": {
                    "key": encode_verify_key_base64(
                        signedjson.key.get_verify_key(OLD_KEY)
                    ),
                    "expired_ts": 1000,
                }
            }
        }
    )
    def test_verify_for_local_server_old_key(self) -> None:
        """Can also use keys in old_signing_keys for verification"""
        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, self.hs.hostname, self.OLD_KEY)

        kr = keyring.Keyring(self.hs)
        d = kr.verify_json_for_server(self.hs.hostname, json1, 0)
        self.get_success(d)

    def test_verify_for_local_server_unknown_key(self) -> None:
        """Local keys that we no longer have should be fetched via the fetcher"""

        # the key we'll sign things with (nb, not known to the Keyring)
        key2 = signedjson.key.generate_signing_key("2")

        # set up a mock fetcher which will return the key
        async def get_keys(
            server_name: str, key_ids: List[str], minimum_valid_until_ts: int
        ) -> Dict[str, FetchKeyResult]:
            self.assertEqual(server_name, self.hs.hostname)
            self.assertEqual(key_ids, [get_key_id(key2)])

            return {get_key_id(key2): FetchKeyResult(get_verify_key(key2), 1200)}

        mock_fetcher = Mock()
        mock_fetcher.get_keys = Mock(side_effect=get_keys)
        kr = keyring.Keyring(
            self.hs, key_fetchers=(StoreKeyFetcher(self.hs), mock_fetcher)
        )

        # sign the json
        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, self.hs.hostname, key2)

        # ... and check we can verify it.
        d = kr.verify_json_for_server(self.hs.hostname, json1, 0)
        self.get_success(d)

    def test_verify_json_dedupes_key_requests(self) -> None:
        """Two requests for the same key should be deduped."""
        key1 = signedjson.key.generate_signing_key("1")

        async def get_keys(
            server_name: str, key_ids: List[str], minimum_valid_until_ts: int
        ) -> Dict[str, FetchKeyResult]:
            # there should only be one request object (with the max validity)
            self.assertEqual(server_name, "server1")
            self.assertEqual(key_ids, [get_key_id(key1)])
            self.assertEqual(minimum_valid_until_ts, 1500)

            return {get_key_id(key1): FetchKeyResult(get_verify_key(key1), 1200)}

        mock_fetcher = Mock()
        mock_fetcher.get_keys = Mock(side_effect=get_keys)
        kr = keyring.Keyring(self.hs, key_fetchers=(mock_fetcher,))

        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, "server1", key1)

        # the first request should succeed; the second should fail because the key
        # has expired
        results = kr.verify_json_objects_for_server(
            [
                (
                    "server1",
                    json1,
                    500,
                ),
                ("server1", json1, 1500),
            ]
        )
        self.assertEqual(len(results), 2)
        self.get_success(results[0])
        e = self.get_failure(results[1], SynapseError).value
        self.assertEqual(e.errcode, "M_UNAUTHORIZED")
        self.assertEqual(e.code, 401)

        # there should have been a single call to the fetcher
        mock_fetcher.get_keys.assert_called_once()

    def test_verify_json_falls_back_to_other_fetchers(self) -> None:
        """If the first fetcher cannot provide a recent enough key, we fall back"""
        key1 = signedjson.key.generate_signing_key("1")

        async def get_keys1(
            server_name: str, key_ids: List[str], minimum_valid_until_ts: int
        ) -> Dict[str, FetchKeyResult]:
            self.assertEqual(server_name, "server1")
            self.assertEqual(key_ids, [get_key_id(key1)])
            self.assertEqual(minimum_valid_until_ts, 1500)
            return {get_key_id(key1): FetchKeyResult(get_verify_key(key1), 800)}

        async def get_keys2(
            server_name: str, key_ids: List[str], minimum_valid_until_ts: int
        ) -> Dict[str, FetchKeyResult]:
            self.assertEqual(server_name, "server1")
            self.assertEqual(key_ids, [get_key_id(key1)])
            self.assertEqual(minimum_valid_until_ts, 1500)
            return {get_key_id(key1): FetchKeyResult(get_verify_key(key1), 1200)}

        mock_fetcher1 = Mock()
        mock_fetcher1.get_keys = Mock(side_effect=get_keys1)
        mock_fetcher2 = Mock()
        mock_fetcher2.get_keys = Mock(side_effect=get_keys2)
        kr = keyring.Keyring(self.hs, key_fetchers=(mock_fetcher1, mock_fetcher2))

        json1: JsonDict = {}
        signedjson.sign.sign_json(json1, "server1", key1)

        results = kr.verify_json_objects_for_server(
            [
                (
                    "server1",
                    json1,
                    1200,
                ),
                (
                    "server1",
                    json1,
                    1500,
                ),
            ]
        )
        self.assertEqual(len(results), 2)
        self.get_success(results[0])
        e = self.get_failure(results[1], SynapseError).value
        self.assertEqual(e.errcode, "M_UNAUTHORIZED")
        self.assertEqual(e.code, 401)

        # there should have been a single call to each fetcher
        mock_fetcher1.get_keys.assert_called_once()
        mock_fetcher2.get_keys.assert_called_once()


@logcontext_clean
class ServerKeyFetcherTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.http_client = Mock()
        hs = self.setup_test_homeserver(federation_http_client=self.http_client)
        return hs

    def test_get_keys_from_server(self) -> None:
        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        SERVER_NAME = "server2"
        fetcher = ServerKeyFetcher(self.hs)
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        # valid response
        response = {
            "server_name": SERVER_NAME,
            "old_verify_keys": {},
            "valid_until_ts": VALID_UNTIL_TS,
            "verify_keys": {
                testverifykey_id: {
                    "key": signedjson.key.encode_verify_key_base64(testverifykey)
                }
            },
        }
        signedjson.sign.sign_json(response, SERVER_NAME, testkey)

        async def get_json(destination: str, path: str, **kwargs: Any) -> JsonDict:
            self.assertEqual(destination, SERVER_NAME)
            self.assertEqual(path, "/_matrix/key/v2/server")
            return response

        self.http_client.get_json.side_effect = get_json

        keys = self.get_success(fetcher.get_keys(SERVER_NAME, ["key1"], 0))
        k = keys[testverifykey_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver1")

        # check that the perspectives store is correctly updated
        key_json = self.get_success(
            self.hs.get_datastores().main.get_server_keys_json_for_remote(
                SERVER_NAME, [testverifykey_id]
            )
        )
        res = key_json[testverifykey_id]
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.added_ts, self.reactor.seconds() * 1000)
        self.assertEqual(res.valid_until_ts, VALID_UNTIL_TS)

        # we expect it to be encoded as canonical json *before* it hits the db
        self.assertEqual(res.key_json, canonicaljson.encode_canonical_json(response))

        # change the server name: the result should be ignored
        response["server_name"] = "OTHER_SERVER"

        keys = self.get_success(fetcher.get_keys(SERVER_NAME, ["key1"], 0))
        self.assertEqual(keys, {})


class PerspectivesKeyFetcherTestCase(unittest.HomeserverTestCase):
    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        self.mock_perspective_server = MockPerspectiveServer()
        self.http_client = Mock()

        config = self.default_config()
        config["trusted_key_servers"] = [
            {
                "server_name": self.mock_perspective_server.server_name,
                "verify_keys": self.mock_perspective_server.get_verify_keys(),
            }
        ]

        return self.setup_test_homeserver(
            federation_http_client=self.http_client, config=config
        )

    def build_perspectives_response(
        self,
        server_name: str,
        signing_key: SigningKey,
        valid_until_ts: int,
    ) -> dict:
        """
        Build a valid perspectives server response to a request for the given key
        """
        verify_key = signedjson.key.get_verify_key(signing_key)
        verifykey_id = "%s:%s" % (verify_key.alg, verify_key.version)

        response = {
            "server_name": server_name,
            "old_verify_keys": {},
            "valid_until_ts": valid_until_ts,
            "verify_keys": {
                verifykey_id: {
                    "key": signedjson.key.encode_verify_key_base64(verify_key)
                }
            },
        }
        # the response must be signed by both the origin server and the perspectives
        # server.
        signedjson.sign.sign_json(response, server_name, signing_key)
        self.mock_perspective_server.sign_response(response)
        return response

    def expect_outgoing_key_query(
        self, expected_server_name: str, expected_key_id: str, response: dict
    ) -> None:
        """
        Tell the mock http client to expect a perspectives-server key query
        """

        async def post_json(
            destination: str, path: str, data: JsonDict, **kwargs: Any
        ) -> JsonDict:
            self.assertEqual(destination, self.mock_perspective_server.server_name)
            self.assertEqual(path, "/_matrix/key/v2/query")

            # check that the request is for the expected key
            q = data["server_keys"]
            self.assertEqual(list(q[expected_server_name].keys()), [expected_key_id])
            return {"server_keys": [response]}

        self.http_client.post_json.side_effect = post_json

    def test_get_keys_from_perspectives(self) -> None:
        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        fetcher = PerspectivesKeyFetcher(self.hs)

        SERVER_NAME = "server2"
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        response = self.build_perspectives_response(
            SERVER_NAME,
            testkey,
            VALID_UNTIL_TS,
        )

        self.expect_outgoing_key_query(SERVER_NAME, "key1", response)

        keys = self.get_success(fetcher.get_keys(SERVER_NAME, ["key1"], 0))
        self.assertIn(testverifykey_id, keys)
        k = keys[testverifykey_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver1")

        # check that the perspectives store is correctly updated
        key_json = self.get_success(
            self.hs.get_datastores().main.get_server_keys_json_for_remote(
                SERVER_NAME, [testverifykey_id]
            )
        )
        res = key_json[testverifykey_id]
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.added_ts, self.reactor.seconds() * 1000)
        self.assertEqual(res.valid_until_ts, VALID_UNTIL_TS)

        self.assertEqual(res.key_json, canonicaljson.encode_canonical_json(response))

    def test_get_multiple_keys_from_perspectives(self) -> None:
        """Check that we can correctly request multiple keys for the same server"""

        fetcher = PerspectivesKeyFetcher(self.hs)

        SERVER_NAME = "server2"

        testkey1 = signedjson.key.generate_signing_key("ver1")
        testverifykey1 = signedjson.key.get_verify_key(testkey1)
        testverifykey1_id = "ed25519:ver1"

        testkey2 = signedjson.key.generate_signing_key("ver2")
        testverifykey2 = signedjson.key.get_verify_key(testkey2)
        testverifykey2_id = "ed25519:ver2"

        VALID_UNTIL_TS = 200 * 1000

        response1 = self.build_perspectives_response(
            SERVER_NAME,
            testkey1,
            VALID_UNTIL_TS,
        )
        response2 = self.build_perspectives_response(
            SERVER_NAME,
            testkey2,
            VALID_UNTIL_TS,
        )

        async def post_json(
            destination: str, path: str, data: JsonDict, **kwargs: str
        ) -> JsonDict:
            self.assertEqual(destination, self.mock_perspective_server.server_name)
            self.assertEqual(path, "/_matrix/key/v2/query")

            # check that the request is for the expected keys
            q = data["server_keys"]

            self.assertEqual(
                list(q[SERVER_NAME].keys()), [testverifykey1_id, testverifykey2_id]
            )
            return {"server_keys": [response1, response2]}

        self.http_client.post_json.side_effect = post_json

        # fire off two separate requests; they should get merged together into a
        # single HTTP hit.
        request1_d = defer.ensureDeferred(
            fetcher.get_keys(SERVER_NAME, [testverifykey1_id], 0)
        )
        request2_d = defer.ensureDeferred(
            fetcher.get_keys(SERVER_NAME, [testverifykey2_id], 0)
        )

        keys1 = self.get_success(request1_d)
        self.assertIn(testverifykey1_id, keys1)
        k = keys1[testverifykey1_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey1)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver1")

        keys2 = self.get_success(request2_d)
        self.assertIn(testverifykey2_id, keys2)
        k = keys2[testverifykey2_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey2)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver2")

        # finally, ensure that only one request was sent
        self.assertEqual(self.http_client.post_json.call_count, 1)

    def test_get_perspectives_own_key(self) -> None:
        """Check that we can get the perspectives server's own keys

        This is slightly complicated by the fact that the perspectives server may
        use different keys for signing notary responses.
        """

        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        fetcher = PerspectivesKeyFetcher(self.hs)

        SERVER_NAME = self.mock_perspective_server.server_name
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        response = self.build_perspectives_response(
            SERVER_NAME, testkey, VALID_UNTIL_TS
        )

        self.expect_outgoing_key_query(SERVER_NAME, "key1", response)

        keys = self.get_success(fetcher.get_keys(SERVER_NAME, ["key1"], 0))
        self.assertIn(testverifykey_id, keys)
        k = keys[testverifykey_id]
        self.assertEqual(k.valid_until_ts, VALID_UNTIL_TS)
        self.assertEqual(k.verify_key, testverifykey)
        self.assertEqual(k.verify_key.alg, "ed25519")
        self.assertEqual(k.verify_key.version, "ver1")

        # check that the perspectives store is correctly updated
        key_json = self.get_success(
            self.hs.get_datastores().main.get_server_keys_json_for_remote(
                SERVER_NAME, [testverifykey_id]
            )
        )
        res = key_json[testverifykey_id]
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res.added_ts, self.reactor.seconds() * 1000)
        self.assertEqual(res.valid_until_ts, VALID_UNTIL_TS)

        self.assertEqual(res.key_json, canonicaljson.encode_canonical_json(response))

    def test_invalid_perspectives_responses(self) -> None:
        """Check that invalid responses from the perspectives server are rejected"""
        # arbitrarily advance the clock a bit
        self.reactor.advance(100)

        SERVER_NAME = "server2"
        testkey = signedjson.key.generate_signing_key("ver1")
        testverifykey = signedjson.key.get_verify_key(testkey)
        testverifykey_id = "ed25519:ver1"
        VALID_UNTIL_TS = 200 * 1000

        def build_response() -> dict:
            return self.build_perspectives_response(
                SERVER_NAME, testkey, VALID_UNTIL_TS
            )

        def get_key_from_perspectives(response: JsonDict) -> Dict[str, FetchKeyResult]:
            fetcher = PerspectivesKeyFetcher(self.hs)
            self.expect_outgoing_key_query(SERVER_NAME, "key1", response)
            return self.get_success(fetcher.get_keys(SERVER_NAME, ["key1"], 0))

        # start with a valid response so we can check we are testing the right thing
        response = build_response()
        keys = get_key_from_perspectives(response)
        k = keys[testverifykey_id]
        self.assertEqual(k.verify_key, testverifykey)

        # remove the perspectives server's signature
        response = build_response()
        del response["signatures"][self.mock_perspective_server.server_name]
        keys = get_key_from_perspectives(response)
        self.assertEqual(keys, {}, "Expected empty dict with missing persp server sig")

        # remove the origin server's signature
        response = build_response()
        del response["signatures"][SERVER_NAME]
        keys = get_key_from_perspectives(response)
        self.assertEqual(keys, {}, "Expected empty dict with missing origin server sig")


def get_key_id(key: SigningKey) -> str:
    """Get the matrix ID tag for a given SigningKey or VerifyKey"""
    return "%s:%s" % (key.alg, key.version)
