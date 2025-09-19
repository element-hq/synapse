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

from typing import Awaitable, Dict, cast

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
from synapse.util.clock import Clock

try:
    import opentracing
    from opentracing.scope_managers.contextvars import ContextVarsScopeManager
except ImportError:
    opentracing = None  # type: ignore
    ContextVarsScopeManager = None  # type: ignore

try:
    import jaeger_client
except ImportError:
    jaeger_client = None  # type: ignore

import logging

from tests.unittest import TestCase

logger = logging.getLogger(__name__)


class TracingScopeTestCase(TestCase):
    """
    Test that our tracing machinery works well in a variety of situations (especially
    with Twisted's runtime and deferreds).

    There's casts throughout this from generic opentracing objects (e.g.
    opentracing.Span) to the ones specific to Jaeger since they have additional
    properties that these tests depend on. This is safe since the only supported
    opentracing backend is Jaeger.
    """

    if opentracing is None:
        skip = "Requires opentracing"  # type: ignore[unreachable]
    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def setUp(self) -> None:
        # since this is a unit test, we don't really want to mess around with the
        # global variables that power opentracing. We create our own tracer instance
        # and test with it.

        scope_manager = ContextVarsScopeManager()
        config = jaeger_client.config.Config(
            config={}, service_name="test", scope_manager=scope_manager
        )

        self._reporter = jaeger_client.reporter.InMemoryReporter()

        self._tracer = config.create_tracer(
            sampler=jaeger_client.ConstSampler(True),
            reporter=self._reporter,
        )

    def test_start_active_span(self) -> None:
        # the scope manager assumes a logging context of some sort.
        with LoggingContext("root context"):
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

        with LoggingContext("root context"):
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
        clock = Clock(
            reactor  # type: ignore[arg-type]
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

        with LoggingContext("root context"):
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

    def test_run_in_background_active_scope_still_available(self) -> None:
        """
        Test that tasks running via `run_in_background` still have access to the
        active tracing scope.

        This is a regression test for a previous Synapse issue where the tracing scope
        would `__exit__` and close before the `run_in_background` task completed and our
        own previous custom `_LogContextScope.close(...)` would clear
        `LoggingContext.scope` preventing further tracing spans from having the correct
        parent.
        """
        reactor = MemoryReactorClock()
        # type-ignore: mypy-zope doesn't seem to recognise that `MemoryReactorClock`
        # implements `ISynapseThreadlessReactor` (combination of the normal Twisted
        # Reactor/Clock interfaces), via inheritance from
        # `twisted.internet.testing.MemoryReactor` and `twisted.internet.testing.Clock`
        clock = Clock(
            reactor  # type: ignore[arg-type]
        )

        scope_map: Dict[str, opentracing.Scope] = {}

        async def async_task() -> None:
            root_scope = scope_map["root"]
            root_context = cast(jaeger_client.SpanContext, root_scope.span.context)

            self.assertEqual(
                self._tracer.active_span,
                root_scope.span,
                "expected to inherit the root tracing scope from where this was run",
            )

            # Return control back to the reactor thread and wait an arbitrary amount
            await clock.sleep(4)

            # This is a key part of what we're testing! In a previous version of
            # Synapse, we would lose the active span at this point.
            self.assertEqual(
                self._tracer.active_span,
                root_scope.span,
                "expected to still have a root tracing scope/span active",
            )

            # For complete-ness sake, let's also trace more sub-tasks here and assert
            # they have the correct span parents as well (root)

            # Start tracing some other sub-task.
            #
            # This is a key part of what we're testing! In a previous version of
            # Synapse, it would have the incorrect span parents.
            scope = start_active_span(
                "task1",
                tracer=self._tracer,
            )
            scope_map["task1"] = scope

            # Ensure the span parent is pointing to the root scope
            context = cast(jaeger_client.SpanContext, scope.span.context)
            self.assertEqual(
                context.parent_id,
                root_context.span_id,
                "expected task1 parent to be the root span",
            )

            # Ensure that the active span is our new sub-task now
            self.assertEqual(self._tracer.active_span, scope.span)
            # Return control back to the reactor thread and wait an arbitrary amount
            await clock.sleep(4)
            # We should still see the active span as the scope wasn't closed yet
            self.assertEqual(self._tracer.active_span, scope.span)
            scope.close()

        async def root() -> None:
            with start_active_span(
                "root span",
                tracer=self._tracer,
                # We will close this off later. We're basically just mimicking the same
                # pattern for how we handle requests. We pass the span off to the
                # request for it to finish.
                finish_on_close=False,
            ) as root_scope:
                scope_map["root"] = root_scope
                self.assertEqual(self._tracer.active_span, root_scope.span)

                # Fire-and-forget a task
                #
                # XXX: The root scope context manager will `__exit__` before this task
                # completes.
                run_in_background(async_task)

                # Because we used `run_in_background`, the active span should still be
                # the root.
                self.assertEqual(self._tracer.active_span, root_scope.span)

            # We shouldn't see any active spans outside of the scope
            self.assertIsNone(self._tracer.active_span)

        with LoggingContext("root context"):
            # Start the test off
            d_root = defer.ensureDeferred(root())

            # Let the tasks complete
            reactor.pump((2,) * 8)
            self.successResultOf(d_root)

            # After we see all of the tasks are done (like a request when it
            # `_finished_processing`), let's finish our root span
            scope_map["root"].span.finish()

            # Sanity check again: We shouldn't see any active spans leftover in this
            # this context.
            self.assertIsNone(self._tracer.active_span)

        # The spans should be reported in order of their finishing: task 1, task 2,
        # root.
        #
        # We use `assertIncludes` just as an easier way to see if items are missing or
        # added. We assert the order just below
        self.assertIncludes(
            set(self._reporter.get_spans()),
            {
                scope_map["task1"].span,
                scope_map["root"].span,
            },
            exact=True,
        )
        # This is where we actually assert the correct order
        self.assertEqual(
            self._reporter.get_spans(),
            [
                scope_map["task1"].span,
                scope_map["root"].span,
            ],
        )

    def test_trace_decorator_sync(self) -> None:
        """
        Test whether we can use `@trace_with_opname` (`@trace`) and `@tag_args`
        with sync functions
        """
        with LoggingContext("root context"):

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
        with LoggingContext("root context"):

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
        with LoggingContext("root context"):

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
        with LoggingContext("root context"):
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
