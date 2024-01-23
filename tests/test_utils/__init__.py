#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2021 The Matrix.org Foundation C.I.C.
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

"""
Utilities for running the unit tests
"""
import json
import sys
import warnings
from binascii import unhexlify
from typing import TYPE_CHECKING, Awaitable, Callable, Tuple, TypeVar

import attr
import zope.interface

from twisted.internet.interfaces import IProtocol
from twisted.python.failure import Failure
from twisted.web.client import ResponseDone
from twisted.web.http import RESPONSES
from twisted.web.http_headers import Headers
from twisted.web.iweb import IResponse

from synapse.types import JsonSerializable

if TYPE_CHECKING:
    from sys import UnraisableHookArgs

TV = TypeVar("TV")


def get_awaitable_result(awaitable: Awaitable[TV]) -> TV:
    """Get the result from an Awaitable which should have completed

    Asserts that the given awaitable has a result ready, and returns its value
    """
    i = awaitable.__await__()
    try:
        next(i)
    except StopIteration as e:
        # awaitable returned a result
        return e.value

    # if next didn't raise, the awaitable hasn't completed.
    raise Exception("awaitable has not yet completed")


def setup_awaitable_errors() -> Callable[[], None]:
    """
    Convert warnings from a non-awaited coroutines into errors.
    """
    warnings.simplefilter("error", RuntimeWarning)

    # State shared between unraisablehook and check_for_unraisable_exceptions.
    unraisable_exceptions = []
    orig_unraisablehook = sys.unraisablehook

    def unraisablehook(unraisable: "UnraisableHookArgs") -> None:
        unraisable_exceptions.append(unraisable.exc_value)

    def cleanup() -> None:
        """
        A method to be used as a clean-up that fails a test-case if there are any new unraisable exceptions.
        """
        sys.unraisablehook = orig_unraisablehook
        if unraisable_exceptions:
            exc = unraisable_exceptions.pop()
            assert exc is not None
            raise exc

    sys.unraisablehook = unraisablehook

    return cleanup


# Type ignore: it does not fully implement IResponse, but is good enough for tests
@zope.interface.implementer(IResponse)
@attr.s(slots=True, frozen=True, auto_attribs=True)
class FakeResponse:  # type: ignore[misc]
    """A fake twisted.web.IResponse object

    there is a similar class at treq.test.test_response, but it lacks a `phrase`
    attribute, and didn't support deliverBody until recently.
    """

    version: Tuple[bytes, int, int] = (b"HTTP", 1, 1)

    # HTTP response code
    code: int = 200

    # body of the response
    body: bytes = b""

    headers: Headers = attr.Factory(Headers)

    @property
    def phrase(self) -> bytes:
        return RESPONSES.get(self.code, b"Unknown Status")

    @property
    def length(self) -> int:
        return len(self.body)

    def deliverBody(self, protocol: IProtocol) -> None:
        protocol.dataReceived(self.body)
        protocol.connectionLost(Failure(ResponseDone()))

    @classmethod
    def json(cls, *, code: int = 200, payload: JsonSerializable) -> "FakeResponse":
        headers = Headers({"Content-Type": ["application/json"]})
        body = json.dumps(payload).encode("utf-8")
        return cls(code=code, body=body, headers=headers)


# A small image used in some tests.
#
# Resolution: 1×1, MIME type: image/png, Extension: png, Size: 67 B
SMALL_PNG = unhexlify(
    b"89504e470d0a1a0a0000000d4948445200000001000000010806"
    b"0000001f15c4890000000a49444154789c63000100000500010d"
    b"0a2db40000000049454e44ae426082"
)
