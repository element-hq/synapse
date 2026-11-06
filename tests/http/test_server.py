#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

from typing import cast
from unittest.mock import Mock

from twisted.web.server import Request

from synapse.http.server import _FinishOnFlushProducer
from synapse.logging.context import (
    LoggingContext,
)
from synapse.logging.opentracing import start_active_span

try:
    import jaeger_client
    import opentracing

    from synapse.logging.scopecontextmanager import LogContextScopeManager
except ImportError:
    jaeger_client = None  # type: ignore

from tests.unittest import TestCase


class FinishOnFlushProducerTestCase(TestCase):
    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def test_paused_response(self) -> None:
        """Test that a paused response is correctly resumed and finished without
        logging errors.

        This is a regression test where writing a large response that gets
        paused by the transport produces the error "Closing scope ... which is
        not the currently-active one"
        """

        ## 1. First, set up the Jaeger tracer.
        config = jaeger_client.config.Config(
            config={}, service_name="test", scope_manager=LogContextScopeManager()
        )
        tracer = config.create_tracer(
            sampler=jaeger_client.ConstSampler(True),
            reporter=jaeger_client.reporter.NullReporter(),
        )
        previous_tracer = opentracing.tracer
        opentracing.set_global_tracer(tracer)
        self.addCleanup(opentracing.set_global_tracer, previous_tracer)

        ## 2. Now create a mock Request.
        request = Mock(spec=Request)

        written_buffer = b""  # The data written to the request

        def write(data: bytes) -> None:
            nonlocal written_buffer
            written_buffer += data

            # Pause on write, as the transport does when its send buffer fills
            # up.
            request.registerProducer.call_args.args[0].pauseProducing()

        request.write.side_effect = write

        ## 3. Now we write the bytes to the request via the
        ## _FinishOnFlushProducer. This should not log any errors or warnings.
        with (
            self.assertNoLogs("synapse.logging.context", "WARNING"),
            self.assertNoLogs("synapse.logging.scopecontextmanager", "ERROR"),
        ):
            buffer_to_write = b"x" * 6000

            # The write happens within the request log context and span
            with (
                LoggingContext(name="request", server_name="test_server"),
                start_active_span("servlet"),
            ):
                producer = _FinishOnFlushProducer(
                    cast(Request, request), buffer_to_write
                )

                # The whole body should have been written in one go, but the
                # request was paused, so it should not have finished yet.
                request.write.assert_called_once()
                self.assertEqual(written_buffer, buffer_to_write)
                request.finish.assert_not_called()

            # Mimic the request resuming the producer. This happens from the
            # reactor and so outside the request log context.
            producer.resumeProducing()

            # Once the transport has drained, the request should be finished.
            request.finish.assert_called_once()
            request.write.assert_called_once()

    def test_unpaused_response(self) -> None:
        """Test that a response which the transport doesn't need to buffer is
        finished immediately.
        """
        request = Mock(spec=Request)

        _FinishOnFlushProducer(cast(Request, request), b"x" * 10)

        request.write.assert_called_once_with(b"x" * 10)
        request.unregisterProducer.assert_called_once()
        request.finish.assert_called_once()
