#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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
import json
from http import HTTPStatus
from io import BytesIO
from typing import Annotated
from unittest.mock import Mock

from pydantic import (
    AfterValidator,
    BaseModel,
    StrictInt,
    StrictStr,
    model_validator,
)
from pydantic_core import PydanticCustomError

from synapse.api.errors import Codes, SynapseError
from synapse.http.servlet import (
    RestServlet,
    parse_json_object_from_request,
    parse_json_value_from_request,
    validate_json_object,
)
from synapse.http.site import SynapseRequest
from synapse.rest.client._base import client_patterns
from synapse.server import HomeServer
from synapse.types import JsonDict
from synapse.util.cancellation import cancellable
from synapse.util.duration import Duration

from tests import unittest
from tests.http.server._base import disconnect_and_assert


def make_request(content: bytes | JsonDict) -> Mock:
    """Make an object that acts enough like a request."""
    request = Mock(spec=["method", "uri", "content"])

    if isinstance(content, dict):
        content = json.dumps(content).encode("utf8")

    request.method = bytes("STUB_METHOD", "ascii")
    request.uri = bytes("/test_stub_uri", "ascii")
    request.content = BytesIO(content)
    return request


class TestServletUtils(unittest.TestCase):
    def test_parse_json_value(self) -> None:
        """Basic tests for parse_json_value_from_request."""
        # Test round-tripping.
        obj = {"foo": 1}
        result1 = parse_json_value_from_request(make_request(obj))
        self.assertEqual(result1, obj)

        # Results don't have to be objects.
        result2 = parse_json_value_from_request(make_request(b'["foo"]'))
        self.assertEqual(result2, ["foo"])

        # Test empty.
        with self.assertRaises(SynapseError):
            parse_json_value_from_request(make_request(b""))

        result3 = parse_json_value_from_request(
            make_request(b""), allow_empty_body=True
        )
        self.assertIsNone(result3)

        # Invalid UTF-8.
        with self.assertRaises(SynapseError):
            parse_json_value_from_request(make_request(b"\xff\x00"))

        # Invalid JSON.
        with self.assertRaises(SynapseError):
            parse_json_value_from_request(make_request(b"foo"))

        with self.assertRaises(SynapseError):
            parse_json_value_from_request(make_request(b'{"foo": Infinity}'))

    def test_parse_json_object(self) -> None:
        """Basic tests for parse_json_object_from_request."""
        # Test empty.
        result = parse_json_object_from_request(
            make_request(b""), allow_empty_body=True
        )
        self.assertEqual(result, {})

        # Test not an object
        with self.assertRaises(SynapseError):
            parse_json_object_from_request(make_request(b'["foo"]'))


class CancellableRestServlet(RestServlet):
    """A `RestServlet` with a mix of cancellable and uncancellable handlers."""

    PATTERNS = client_patterns("/sleep$")

    def __init__(self, hs: HomeServer):
        super().__init__()
        self.clock = hs.get_clock()

    @cancellable
    async def on_GET(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await self.clock.sleep(Duration(seconds=1))
        return HTTPStatus.OK, {"result": True}

    async def on_POST(self, request: SynapseRequest) -> tuple[int, JsonDict]:
        await self.clock.sleep(Duration(seconds=1))
        return HTTPStatus.OK, {"result": True}


class TestRestServletCancellation(unittest.HomeserverTestCase):
    """Tests for `RestServlet` cancellation."""

    servlets = [
        lambda hs, http_server: CancellableRestServlet(hs).register(http_server)
    ]

    def test_cancellable_disconnect(self) -> None:
        """Test that handlers with the `@cancellable` flag can be cancelled."""
        channel = self.make_request("GET", "/sleep", await_result=False)
        disconnect_and_assert(
            self.reactor,
            channel,
            expect_cancellation=True,
            expected_body={"error": "Request cancelled", "errcode": Codes.UNKNOWN},
        )

    def test_uncancellable_disconnect(self) -> None:
        """Test that handlers without the `@cancellable` flag cannot be cancelled."""
        channel = self.make_request("POST", "/sleep", await_result=False)
        disconnect_and_assert(
            self.reactor,
            channel,
            expect_cancellation=False,
            expected_body={"result": True},
        )


def _validate_short(value: str) -> str:
    """Validator raising a custom error with an `errcode` in its context."""
    if len(value) > 3:
        raise PydanticCustomError(
            "string_too_long", "Too long", {"errcode": Codes.KEY_TOO_LARGE}
        )
    return value


class PydanticErrorFormatingTestCase(unittest.TestCase):
    """Tests the formatting of pydantic error messages.

    The default pydantic error messages are not user-friendly and leak internal
    details. A custom error formatter is surprisingly tricky to get right and so
    we add tests for the various cases. We do not care about the exact wording
    though.
    """

    class TestModel(BaseModel):
        """Test model with various field different field types"""

        class _Inner(BaseModel):
            count: StrictInt

        name: StrictStr
        inner: _Inner | None = None
        ids: list[StrictInt] = []
        either: StrictStr | StrictInt = "x"
        ids_or_name: list[StrictInt] | StrictStr = "x"
        short_names: list[
            Annotated[
                StrictStr,
                AfterValidator(_validate_short),
            ]
        ] = []
        limit: StrictInt | None = None

        @model_validator(mode="after")
        def check_limit(self) -> "PydanticErrorFormatingTestCase.TestModel":
            # A custom validator to test formatting custom error messages
            if self.limit is not None and self.limit > 10:
                raise ValueError("limit must be at most 10.")
            return self

    def _validate(self, body: dict) -> SynapseError:
        """Helper method to validate a bad JSON body against the test model."""
        with self.assertRaises(SynapseError) as cm:
            validate_json_object(body, self.TestModel)
        self.assertEqual(cm.exception.code, HTTPStatus.BAD_REQUEST)
        return cm.exception

    def test_missing_field(self) -> None:
        """Test missing field"""
        e = self._validate({})
        self.assertEqual(e.errcode, Codes.MISSING_PARAM)
        self.assertEqual(e.msg, "Missing required field '.name'")

    def test_nested_path_and_list_index(self) -> None:
        """Test error messages for nested paths and list indices."""
        e = self._validate({"name": "n", "inner": {"count": "x"}, "ids": [1, "two"]})
        self.assertEqual(e.errcode, Codes.BAD_JSON)
        self.assertEqual(
            e.msg,
            "'.inner.count': Input should be a valid integer; '.ids[1]': Input should be a valid integer",
        )

    def test_object_expected_does_not_leak_class_name(self) -> None:
        """Test that object expected errors do not leak internal class names."""

        e = self._validate({"name": "n", "inner": "oops"})
        self.assertEqual(e.msg, "'.inner' must be an object")
        self.assertNotIn("_Inner", e.msg)

    def test_union_drops_branch_tags(self) -> None:
        """Test that union type errors do not include branch tags."""

        e = self._validate({"name": "n", "either": 1.5})
        self.assertEqual(
            e.msg,
            "'.either': Input should be a valid string; '.either': Input should be a valid integer",
        )

    def test_union_list_branch_keeps_index(self) -> None:
        """Test that errors inside a list branch of a union keep the list index,
        even though the branch tag itself is dropped."""

        e = self._validate({"name": "n", "ids_or_name": [1, "two"]})
        self.assertEqual(
            e.msg,
            "'.ids_or_name[1]': Input should be a valid integer; '.ids_or_name': Input should be a valid string",
        )

    def test_custom_errcode_from_error_context(self) -> None:
        """Test that a custom validator can choose the errcode via an `errcode` in
        the error context, as long as every error agrees on it."""

        e = self._validate({"name": "n", "short_names": ["ok", "toolong", "nope!"]})
        self.assertEqual(e.errcode, Codes.KEY_TOO_LARGE)
        self.assertEqual(
            e.msg, "'.short_names[1]': Too long; '.short_names[2]': Too long"
        )

        # Mixed with an error that has no custom errcode: fall back to BAD_JSON.
        e = self._validate({"name": "n", "short_names": ["toolong"], "ids": ["x"]})
        self.assertEqual(e.errcode, Codes.BAD_JSON)

    def test_custom_validator_message_used_verbatim(self) -> None:
        """Test that custom validator messages are used verbatim.

        They don't include the 'path' as the pydantic error doesn't provide one
        in this case.
        """

        e = self._validate({"name": "n", "limit": 11})
        self.assertEqual(e.errcode, Codes.INVALID_PARAM)
        self.assertEqual(e.msg, "limit must be at most 10.")
