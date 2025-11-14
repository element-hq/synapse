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

from typing import Awaitable, cast

from twisted.internet import defer
from twisted.internet.testing import MemoryReactorClock

from synapse.logging.context import (
    LoggingContext,
    make_deferred_yieldable,
    run_in_background,
)
from synapse.logging.opentracing import (
    start_active_span,
    start_active_span_follows_from,
    tag_args,
    trace_with_opname,
)
from synapse.metrics.background_process_metrics import run_as_background_process
from synapse.util.clock import Clock

from tests.server import get_clock

try:
    import jaeger_client
except ImportError:
    jaeger_client = None  # type: ignore


try:
    import opentracing

    from synapse.logging.scopecontextmanager import LogContextScopeManager
except ImportError:
    opentracing = None  # type: ignore
    LogContextScopeManager = None  # type: ignore

import logging

from tests.unittest import TestCase

logger = logging.getLogger(__name__)


class LogContextScopeManagerTestCase(TestCase):
    """
    Test that our tracing machinery works well in a variety of situations (especially
    with Twisted's runtime and deferreds).

    There's casts throughout this from generic opentracing objects (e.g.
    opentracing.Span) to the ones specific to Jaeger since they have additional
    properties that these tests depend on. This is safe since the only supported
    opentracing backend is Jaeger.
    """

    if opentracing is None or LogContextScopeManager is None:
        skip = "Requires opentracing"  # type: ignore[unreachable]
    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def setUp(self) -> None:
        # since this is a unit test, we don't really want to mess around with the
        # global variables that power opentracing. We create our own tracer instance
        # and test with it.

        config = jaeger_client.config.Config(
            config={}, service_name="test", scope_manager=LogContextScopeManager()
        )

        self._reporter = jaeger_client.reporter.InMemoryReporter()

        self._tracer = config.create_tracer(
            sampler=jaeger_client.ConstSampler(True),
            reporter=self._reporter,
        )

    def test_start_active_span(self) -> None:
        # the scope manager assumes a logging context of some sort.
        with LoggingContext(name="root context", server_name="test_server"):
            self.assertIsNone(self._tracer.active_span)

            # start_active_span should start and activate a span.
            scope = start_active_span("span", tracer=self._tracer)
            span = cast(jaeger_client.Span, scope.span)
            self.assertEqual(self._tracer.active_span, span)
            self.assertIsNotNone(span.start_time)

            # entering the context doesn't actually do a whole lot.
            with scope as ctx:
                self.assertIs(ctx, scope)
                self.assertEqual(self._tracer.active_span, span)

            # ... but leaving it unsets the active span, and finishes the span.
            self.assertIsNone(self._tracer.active_span)
            self.assertIsNotNone(span.end_time)

        # the span should have been reported
        self.assertEqual(self._reporter.get_spans(), [span])

    def test_nested_spans(self) -> None:
        """Starting two spans off inside each other should work"""

        with LoggingContext(name="root context", server_name="test_server"):
            with start_active_span("root span", tracer=self._tracer) as root_scope:
                self.assertEqual(self._tracer.active_span, root_scope.span)
                root_context = cast(jaeger_client.SpanContext, root_scope.span.context)

                scope1 = start_active_span(
                    "child1",
                    tracer=self._tracer,
                )
                self.assertEqual(
                    self._tracer.active_span, scope1.span, "child1 was not activated"
                )
                context1 = cast(jaeger_client.SpanContext, scope1.span.context)
                self.assertEqual(context1.parent_id, root_context.span_id)

                scope2 = start_active_span_follows_from(
                    "child2",
                    contexts=(scope1,),
                    tracer=self._tracer,
                )
                self.assertEqual(self._tracer.active_span, scope2.span)
                context2 = cast(jaeger_client.SpanContext, scope2.span.context)
                self.assertEqual(context2.parent_id, context1.span_id)

                with scope1, scope2:
                    pass

                # the root scope should be restored
                self.assertEqual(self._tracer.active_span, root_scope.span)
                span2 = cast(jaeger_client.Span, scope2.span)
                span1 = cast(jaeger_client.Span, scope1.span)
                self.assertIsNotNone(span2.end_time)
                self.assertIsNotNone(span1.end_time)

            self.assertIsNone(self._tracer.active_span)

        # the spans should be reported in order of their finishing.
        self.assertEqual(
            self._reporter.get_spans(), [scope2.span, scope1.span, root_scope.span]
        )

    def test_overlapping_spans(self) -> None:
        """Overlapping spans which are not neatly nested should work"""
        reactor = MemoryReactorClock()
        # type-ignore: mypy-zope doesn't seem to recognise that `MemoryReactorClock`
        # implements `ISynapseThreadlessReactor` (combination of the normal Twisted
        # Reactor/Clock interfaces), via inheritance from
        # `twisted.internet.testing.MemoryReactor` and `twisted.internet.testing.Clock`
        # Ignore `multiple-internal-clocks` linter error here since we are creating a `Clock`
        # for testing purposes.
        clock = Clock(  # type: ignore[multiple-internal-clocks]
            reactor,  # type: ignore[arg-type]
            server_name="test_server",
        )

        scopes = []

        async def task(i: int) -> None:
            scope = start_active_span(
                f"task{i}",
                tracer=self._tracer,
            )
            scopes.append(scope)

            self.assertEqual(self._tracer.active_span, scope.span)
            await clock.sleep(4)
            self.assertEqual(self._tracer.active_span, scope.span)
            scope.close()

        async def root() -> None:
            with start_active_span("root span", tracer=self._tracer) as root_scope:
                self.assertEqual(self._tracer.active_span, root_scope.span)
                scopes.append(root_scope)

                d1 = run_in_background(task, 1)
                await clock.sleep(2)
                d2 = run_in_background(task, 2)

                # because we did run_in_background, the active span should still be the
                # root.
                self.assertEqual(self._tracer.active_span, root_scope.span)

                await make_deferred_yieldable(
                    defer.gatherResults([d1, d2], consumeErrors=True)
                )

                self.assertEqual(self._tracer.active_span, root_scope.span)

        with LoggingContext(name="root context", server_name="test_server"):
            # start the test off
            d1 = defer.ensureDeferred(root())

            # let the tasks complete
            reactor.pump((2,) * 8)

            self.successResultOf(d1)
            self.assertIsNone(self._tracer.active_span)

        # the spans should be reported in order of their finishing: task 1, task 2,
        # root.
        self.assertEqual(
            self._reporter.get_spans(),
            [scopes[1].span, scopes[2].span, scopes[0].span],
        )

    def test_trace_decorator_sync(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with sync functions
        """
        with LoggingContext(name="root context", server_name="test_server"):

            @trace_with_opname("fixture_sync_func", tracer=self._tracer)
            @tag_args
            def fixture_sync_func() -> str:
                return "foo"

            result = fixture_sync_func()
            self.assertEqual(result, "foo")

        # the span should have been reported
        self.assertEqual(
            [span.operation_name for span in self._reporter.get_spans()],
            ["fixture_sync_func"],
        )

    def test_trace_decorator_deferred(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with functions that return deferreds
        """
        with LoggingContext(name="root context", server_name="test_server"):

            @trace_with_opname("fixture_deferred_func", tracer=self._tracer)
            @tag_args
            def fixture_deferred_func() -> "defer.Deferred[str]":
                d1: defer.Deferred[str] = defer.Deferred()
                d1.callback("foo")
                return d1

            result_d1 = fixture_deferred_func()

            self.assertEqual(self.successResultOf(result_d1), "foo")

        # the span should have been reported
        self.assertEqual(
            [span.operation_name for span in self._reporter.get_spans()],
            ["fixture_deferred_func"],
        )

    def test_trace_decorator_async(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with async functions
        """
        with LoggingContext(name="root context", server_name="test_server"):

            @trace_with_opname("fixture_async_func", tracer=self._tracer)
            @tag_args
            async def fixture_async_func() -> str:
                return "foo"

            d1 = defer.ensureDeferred(fixture_async_func())

            self.assertEqual(self.successResultOf(d1), "foo")

        # the span should have been reported
        self.assertEqual(
            [span.operation_name for span in self._reporter.get_spans()],
            ["fixture_async_func"],
        )

    def test_trace_decorator_awaitable_return(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with functions that return an awaitable (e.g. a coroutine)
        """
        with LoggingContext(name="root context", server_name="test_server"):
            # Something we can return without `await` to get a coroutine
            async def fixture_async_func() -> str:
                return "foo"

            # The actual kind of function we want to test that returns an awaitable
            @trace_with_opname("fixture_awaitable_return_func", tracer=self._tracer)
            @tag_args
            def fixture_awaitable_return_func() -> Awaitable[str]:
                return fixture_async_func()

            # Something we can run with `defer.ensureDeferred(runner())` and pump the
            # whole async tasks through to completion.
            async def runner() -> str:
                return await fixture_awaitable_return_func()

            d1 = defer.ensureDeferred(runner())

            self.assertEqual(self.successResultOf(d1), "foo")

        # the span should have been reported
        self.assertEqual(
            [span.operation_name for span in self._reporter.get_spans()],
            ["fixture_awaitable_return_func"],
        )

    async def test_run_as_background_process_standalone(self) -> None:
        """
        Test to make sure that the background process work starts its own trace.
        """
        reactor, clock = get_clock()

        callback_finished = False
        active_span_in_callback: jaeger_client.Span | None = None

        async def bg_task() -> None:
            nonlocal callback_finished, active_span_in_callback
            try:
                assert isinstance(self._tracer.active_span, jaeger_client.Span)
                active_span_in_callback = self._tracer.active_span
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        # type-ignore: We ignore because the point is to test the bare function
        run_as_background_process(  # type: ignore[untracked-background-process]
            desc="some-bg-task",
            server_name="test_server",
            func=bg_task,
            test_only_tracer=self._tracer,
        )

        # Now wait for the background process to finish
        while not callback_finished:
            await clock.sleep(0)

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        self.assertEqual(
            active_span_in_callback.operation_name if active_span_in_callback else None,
            "bgproc.some-bg-task",
            "expected a new span to be started for the background task",
        )

        # The spans should be reported in order of their finishing.
        #
        # We use `assertIncludes` just as an easier way to see if items are missing or
        # added. We assert the order just below
        actual_spans = [span.operation_name for span in self._reporter.get_spans()]
        expected_spans = ["bgproc.some-bg-task"]
        self.assertIncludes(
            set(actual_spans),
            set(expected_spans),
            exact=True,
        )
        # This is where we actually assert the correct order
        self.assertEqual(
            actual_spans,
            expected_spans,
        )

    async def test_run_as_background_process_cross_link(self) -> None:
        """
        Test to make sure that the background process work has its own trace and is
        disconnected from any currently active trace (like a request). But we still have
        cross-links between the two traces if there was already an active trace/span when
        we kicked off the background process.
        """
        reactor, clock = get_clock()

        callback_finished = False
        active_span_in_callback: jaeger_client.Span | None = None

        async def bg_task() -> None:
            nonlocal callback_finished, active_span_in_callback
            try:
                assert isinstance(self._tracer.active_span, jaeger_client.Span)
                active_span_in_callback = self._tracer.active_span
            finally:
                # When exceptions happen, we still want to mark the callback as finished
                # so that the test can complete and we see the underlying error.
                callback_finished = True

        with LoggingContext(name="some-request", server_name="test_server"):
            with start_active_span(
                "some-request",
                tracer=self._tracer,
            ):
                # type-ignore: We ignore because the point is to test the bare function
                run_as_background_process(  # type: ignore[untracked-background-process]
                    desc="some-bg-task",
                    server_name="test_server",
                    func=bg_task,
                    test_only_tracer=self._tracer,
                )

        # Now wait for the background process to finish
        while not callback_finished:
            await clock.sleep(0)

        self.assertTrue(
            callback_finished,
            "Callback never finished which means the test probably didn't wait long enough",
        )

        # We start `bgproc.some-bg-task` and `bgproc_child.some-bg-task` (see
        # `run_as_background_process` implementation for why). Either is fine but for
        # now we expect the child as its the innermost one that was started.
        self.assertEqual(
            active_span_in_callback.operation_name if active_span_in_callback else None,
            "bgproc_child.some-bg-task",
            "expected a new span to be started for the background task",
        )

        # The spans should be reported in order of their finishing.
        #
        # We use `assertIncludes` just as an easier way to see if items are missing or
        # added. We assert the order just below
        actual_spans = [span.operation_name for span in self._reporter.get_spans()]
        expected_spans = [
            "start_bgproc.some-bg-task",
            "bgproc_child.some-bg-task",
            "bgproc.some-bg-task",
            "some-request",
        ]
        self.assertIncludes(
            set(actual_spans),
            set(expected_spans),
            exact=True,
        )
        # This is where we actually assert the correct order
        self.assertEqual(
            actual_spans,
            expected_spans,
        )

        span_map = {span.operation_name: span for span in self._reporter.get_spans()}
        span_id_to_friendly_name = {
            span.span_id: span.operation_name for span in self._reporter.get_spans()
        }

        def get_span_friendly_name(span_id: int | None) -> str:
            if span_id is None:
                return "None"

            return span_id_to_friendly_name.get(span_id, f"unknown span {span_id}")

        # Ensure the background process trace/span is disconnected from the request
        # trace/span.
        self.assertNotEqual(
            get_span_friendly_name(span_map["bgproc.some-bg-task"].parent_id),
            get_span_friendly_name(span_map["some-request"].span_id),
        )

        # We should see a cross-link in the request trace pointing to the background
        # process trace.
        #
        # Make sure `start_bgproc.some-bg-task` is part of the request trace
        self.assertEqual(
            get_span_friendly_name(span_map["start_bgproc.some-bg-task"].parent_id),
            get_span_friendly_name(span_map["some-request"].span_id),
        )
        # And has some references to the background process trace
        self.assertIncludes(
            {
                f"{reference.type}:{get_span_friendly_name(reference.referenced_context.span_id)}"
                if isinstance(reference.referenced_context, jaeger_client.SpanContext)
                else f"{reference.type}:None"
                for reference in (
                    span_map["start_bgproc.some-bg-task"].references or []
                )
            },
            {
                f"follows_from:{get_span_friendly_name(span_map['bgproc.some-bg-task'].span_id)}"
            },
            exact=True,
        )

        # We should see a cross-link in the background process trace pointing to the
        # request trace that kicked off the work.
        #
        # Make sure `start_bgproc.some-bg-task` is part of the request trace
        self.assertEqual(
            get_span_friendly_name(span_map["bgproc_child.some-bg-task"].parent_id),
            get_span_friendly_name(span_map["bgproc.some-bg-task"].span_id),
        )
        # And has some references to the background process trace
        self.assertIncludes(
            {
                f"{reference.type}:{get_span_friendly_name(reference.referenced_context.span_id)}"
                if isinstance(reference.referenced_context, jaeger_client.SpanContext)
                else f"{reference.type}:None"
                for reference in (
                    span_map["bgproc_child.some-bg-task"].references or []
                )
            },
            {
                f"follows_from:{get_span_friendly_name(span_map['some-request'].span_id)}"
            },
            exact=True,
        )
