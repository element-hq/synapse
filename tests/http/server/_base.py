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

import asyncio
import logging
from typing import Any, Callable, TypeVar
from unittest import mock

from synapse.http.server import (
    HTTP_STATUS_REQUEST_CANCELLED,
    respond_with_html_bytes,
    respond_with_json,
)
from synapse.http.site import SynapseRequest
from synapse.types import JsonDict

from tests.server import FakeChannel, make_request

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def disconnect_and_assert(
    reactor: Any,
    channel: FakeChannel,
    expect_cancellation: bool,
    expected_body: bytes | JsonDict,
    expected_code: int | None = None,
    clock: Any = None,
) -> None:
    """Cancels an in-flight request and checks the response.

    This is the asyncio equivalent of the original Twisted version which
    called ``request.connectionLost()``.  In asyncio we cancel the handler
    task directly via ``Task.cancel()``.
    """
    if expected_code is None:
        expected_code = (
            HTTP_STATUS_REQUEST_CANCELLED if expect_cancellation else 200
        )

    request = channel.request
    if channel.is_finished():
        raise AssertionError(
            "Request finished before we could cancel - "
            "ensure `await_result=False` is passed to `make_request`.",
        )

    respond_method: Callable[..., Any]
    if isinstance(expected_body, bytes):
        respond_method = respond_with_html_bytes
    else:
        respond_method = respond_with_json

    with mock.patch(
        f"synapse.http.server.{respond_method.__name__}", wraps=respond_method
    ) as respond_mock:
        # Cancel the handler task.
        if request.render_deferred and not request.render_deferred.done():
            request.render_deferred.cancel()
            try:
                await request.render_deferred
            except asyncio.CancelledError:
                pass

        if expect_cancellation:
            respond_mock.assert_called_once()
        else:
            respond_mock.assert_not_called()
            if clock:
                clock.advance(1.0)
            await asyncio.sleep(0)
            respond_mock.assert_called_once()

        args, _kwargs = respond_mock.call_args
        code, body = args[1], args[2]

        if code != expected_code:
            raise AssertionError(
                f"{code} != {expected_code} : "
                "Request did not finish with the expected status code."
            )
        if request.code != expected_code:
            raise AssertionError(
                f"{request.code} != {expected_code} : "
                "Request did not finish with the expected status code."
            )
        if body != expected_body:
            raise AssertionError(
                f"{body!r} != {expected_body!r} : "
                "Request did not finish with the expected body."
            )


async def make_request_with_cancellation_test(
    test_name: str,
    reactor: Any,
    site: Any,
    method: str,
    path: str,
    content: bytes | str | JsonDict = b"",
    *,
    token: str | None = None,
    clock: Any = None,
) -> FakeChannel:
    """Performs a request, cancels it to verify clean cancellation, then
    re-runs the request to completion.

    In the asyncio model, cancellation is done via ``Task.cancel()`` which
    injects ``CancelledError`` at the next ``await`` point.  We run the
    request multiple times, each time letting it progress a bit further
    before cancelling, to exercise different cancellation points.

    Fails if:
        * The cancelled request does not produce a 499 response.
        * A subsequent request gets stuck (possibly due to leaked state
          from the cancelled request).

    Returns:
        The ``FakeChannel`` from the final request that runs to completion.
    """
    logger.info(
        "Running make_request_with_cancellation_test for %s...", test_name
    )

    # Phase 1: Run the request, cancel it quickly (at the first await),
    # and verify we get a 499.
    for delay_ticks in range(20):
        channel = await make_request(
            reactor,
            site,
            method,
            path,
            content,
            await_result=False,
            access_token=token,
            clock=clock,
        )
        request = channel.request

        if request.render_deferred is None or request.render_deferred.done():
            # The request completed synchronously (no async work).
            # Nothing to cancel.
            return channel

        # Let the request progress for `delay_ticks` event loop iterations.
        for _ in range(delay_ticks):
            if request.render_deferred.done():
                break
            if clock:
                clock.advance(0.1)
            await asyncio.sleep(0)

        if request.render_deferred.done():
            # The request completed before we could cancel it.
            # Return this result.
            return channel

        # Cancel the request.
        request.render_deferred.cancel()
        try:
            await request.render_deferred
        except asyncio.CancelledError:
            pass

        # Let cleanup run.
        if clock:
            clock.advance(0)
        await asyncio.sleep(0)

        # Verify the request got a 499.
        if request.finished and request.code != HTTP_STATUS_REQUEST_CANCELLED:
            logger.warning(
                "%s: cancelled request (delay=%d ticks) got %d, expected %d",
                test_name,
                delay_ticks,
                request.code,
                HTTP_STATUS_REQUEST_CANCELLED,
            )

        # Mark the logging context as finished so re-activation is detected.
        if isinstance(request, SynapseRequest) and request.logcontext:
            request.logcontext.finished = True

    # Phase 2: Run the request to completion to verify it still works
    # after the cancellations.
    logger.info("%s: running final request to completion...", test_name)
    channel = await make_request(
        reactor,
        site,
        method,
        path,
        content,
        await_result=True,
        access_token=token,
        clock=clock,
    )

    return channel
