#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

"""Tests for reporting opentracing spans from Rust."""

import socket
from typing import Callable, cast

from twisted.internet.testing import MemoryReactor

from synapse.logging.context import LoggingContext
from synapse.logging.opentracing import _enable_rust_tracing, start_active_span
from synapse.logging.rust_tracing_metrics import rust_tracing_collector
from synapse.server import HomeServer
from synapse.synapse_rust.opentracing import (
    captured_span_for_test,
    set_tracing_enabled_for_test,
    span_in_blocking_task_for_test,
    span_in_task_for_test,
    span_on_thread_for_test,
)
from synapse.util.clock import Clock

from tests.unittest import HomeserverTestCase, TestCase

try:
    import jaeger_client
    import opentracing
    from jaeger_client.sampler import ConstSampler, ProbabilisticSampler
    from jaeger_client.thrift_gen.agent.Agent import emitBatch_args
    from jaeger_client.thrift_gen.jaeger import ttypes
    from thrift.protocol.TCompactProtocol import (  # type: ignore[import-untyped]
        TCompactProtocol,
    )
    from thrift.transport.TTransport import (  # type: ignore[import-untyped]
        TMemoryBuffer,
    )

    from synapse.logging.scopecontextmanager import LogContextScopeManager
except ImportError:
    jaeger_client = None  # type: ignore
    LogContextScopeManager = None  # type: ignore


def _context(span: "opentracing.Span") -> "jaeger_client.SpanContext":
    return cast(jaeger_client.SpanContext, span.context)


def _make_tracer(sampled: bool = True) -> "jaeger_client.Tracer":
    """A Jaeger tracer that keeps its spans in memory and uses the logcontext
    scope manager, like the real one."""
    config = jaeger_client.config.Config(
        config={}, service_name="test", scope_manager=LogContextScopeManager()
    )
    return config.create_tracer(
        sampler=jaeger_client.ConstSampler(sampled),
        reporter=jaeger_client.reporter.InMemoryReporter(),
    )


class SpanCaptureTestCase(TestCase):
    """Rust reads the active span's context off the current logcontext when it
    captures a handle for a task."""

    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def setUp(self) -> None:
        self._tracer = _make_tracer()
        set_tracing_enabled_for_test(True)
        self.addCleanup(set_tracing_enabled_for_test, False)

    def test_captures_active_span(self) -> None:
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("outer", tracer=self._tracer) as scope:
                span = cast(jaeger_client.Span, scope.span)
                span.set_baggage_item("user", "@alice:test")

                captured = captured_span_for_test()

        assert captured is not None
        trace_id, span_id, flags, baggage = captured
        self.assertEqual(trace_id, _context(span).trace_id)
        self.assertEqual(span_id, _context(span).span_id)
        self.assertEqual(flags, _context(span).flags)
        self.assertEqual(flags & 1, 1)
        self.assertEqual(baggage, {"user": "@alice:test"})

    def test_captures_innermost_span(self) -> None:
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("outer", tracer=self._tracer) as outer:
                with start_active_span("inner", tracer=self._tracer) as inner:
                    captured = captured_span_for_test()

                # Back to the outer span once the inner one closes.
                outer_captured = captured_span_for_test()

        assert captured is not None
        self.assertEqual(captured[1], _context(inner.span).span_id)
        assert outer_captured is not None
        self.assertEqual(outer_captured[1], _context(outer.span).span_id)
        self.assertEqual(outer_captured[0], captured[0])

    def test_unsampled_span_keeps_its_flags(self) -> None:
        tracer = _make_tracer(sampled=False)
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("outer", tracer=tracer) as scope:
                captured = captured_span_for_test()

        assert captured is not None
        self.assertEqual(captured[1], _context(scope.span).span_id)
        self.assertEqual(captured[2] & 1, 0)

    def test_nothing_without_a_span(self) -> None:
        # The sentinel context.
        self.assertIsNone(captured_span_for_test())

        # A logcontext with no active span.
        with LoggingContext(name="ctx", server_name="test_server"):
            self.assertIsNone(captured_span_for_test())

    def test_nothing_while_disabled(self) -> None:
        set_tracing_enabled_for_test(False)
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("outer", tracer=self._tracer):
                self.assertIsNone(captured_span_for_test())


class BlockingTaskSpanTestCase(HomeserverTestCase):
    """The `_blocking` entry points run their futures with the caller's span
    as the parent, the same as the deferred ones."""

    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        set_tracing_enabled_for_test(True)
        self.addCleanup(set_tracing_enabled_for_test, False)

    def test_blocking_task_sees_the_span(self) -> None:
        runtime = self.hs.get_rust_runtime()
        tracer = _make_tracer()
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("outer", tracer=tracer) as scope:
                span = span_in_blocking_task_for_test(runtime)

            # No span once the scope has closed.
            self.assertIsNone(span_in_blocking_task_for_test(runtime))

        assert span is not None
        self.assertEqual(span[0], _context(scope.span).trace_id)
        self.assertEqual(span[1], _context(scope.span).span_id)


class EnableRustTracingTestCase(HomeserverTestCase):
    """`_enable_rust_tracing`, which `init_tracer` calls, hands the Python
    tracer's settings to the Rust runtime."""

    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        # Enabling tracing is process-wide: switch capture off again so it
        # does not leak into other tests.
        self.addCleanup(set_tracing_enabled_for_test, False)

    def _enable(self, sampler: "jaeger_client.sampler.Sampler") -> None:
        config = jaeger_client.config.Config(
            config={
                "local_agent": {"reporting_host": "127.0.0.2", "reporting_port": 6000},
                "tags": {"instance": "master"},
            },
            service_name="test service",
        )
        tracer = config.create_tracer(
            sampler=sampler, reporter=jaeger_client.reporter.InMemoryReporter()
        )
        _enable_rust_tracing(self.hs, config, tracer)
        # Detach this homeserver's reporter from the process-wide `tracing`
        # subscriber, so later tests can attach theirs.
        self.addCleanup(self.hs.get_rust_runtime().shutdown_tracing_for_test)

    def test_enable(self) -> None:
        runtime = self.hs.get_rust_runtime()
        self.assertIsNone(runtime.tracing_config_for_test())

        self._enable(ConstSampler(True))
        settings = runtime.tracing_config_for_test()
        assert settings is not None
        service_name, process_tags, host, port, sampler = settings
        self.assertEqual(service_name, "test service")
        tags = dict(process_tags)
        self.assertEqual(tags["instance"], "master")
        self.assertIn("hostname", tags)
        self.assertIn("jaeger.version", tags)
        self.assertEqual((host, port), ("127.0.0.2", 6000))
        self.assertEqual(sampler, "Const(true)")

        # A second call is ignored rather than raising.
        self._enable(ProbabilisticSampler(0.5))
        settings = runtime.tracing_config_for_test()
        assert settings is not None
        self.assertEqual(settings[4], "Const(true)")

    def test_probabilistic_sampler(self) -> None:
        self._enable(ProbabilisticSampler(0.25))
        settings = self.hs.get_rust_runtime().tracing_config_for_test()
        assert settings is not None
        self.assertEqual(settings[4], "Probabilistic(0.25)")

    def test_unsupported_sampler(self) -> None:
        self._enable(jaeger_client.sampler.RateLimitingSampler(10))
        settings = self.hs.get_rust_runtime().tracing_config_for_test()
        assert settings is not None
        self.assertEqual(settings[4], "Never")


def _signed(value: int) -> int:
    """An ID as thrift's signed i64 carries it."""
    return value - (1 << 64) if value >= (1 << 63) else value


def _decode_emit_batch(data: bytes) -> "ttypes.Batch":
    """Decode an `emitBatch` datagram the way the Jaeger agent does."""
    protocol = TCompactProtocol(TMemoryBuffer(data))
    name, _message_type, _seq_id = protocol.readMessageBegin()
    assert name == "emitBatch", name
    args = emitBatch_args()
    args.read(protocol)
    return args.batch


class ReporterRoundTripTestCase(HomeserverTestCase):
    """Spans reported from Rust reach the Jaeger agent's UDP port in a form
    jaeger-client's own thrift code decodes."""

    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.addCleanup(set_tracing_enabled_for_test, False)

        self.agent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.agent.close)
        self.agent.bind(("127.0.0.1", 0))
        self.agent.settimeout(5)
        port = self.agent.getsockname()[1]

        config = jaeger_client.config.Config(
            config={
                "local_agent": {"reporting_host": "127.0.0.1", "reporting_port": port},
                "tags": {"instance": "master"},
            },
            service_name="test service",
            scope_manager=LogContextScopeManager(),
        )
        tracer = config.create_tracer(
            sampler=ConstSampler(True),
            reporter=jaeger_client.reporter.InMemoryReporter(),
        )
        _enable_rust_tracing(hs, config, tracer)
        self.runtime = hs.get_rust_runtime()
        # Detach this homeserver's reporter from the process-wide `tracing`
        # subscriber, so later tests record nothing.
        self.addCleanup(self.runtime.shutdown_tracing_for_test)
        self.tracer = tracer

    def _receive(self) -> "ttypes.Batch":
        data, _ = self.agent.recvfrom(70_000)
        return _decode_emit_batch(data)

    def test_round_trip(self) -> None:
        trace_id = (1 << 100) + 5
        span_id = (1 << 63) + 7
        self.runtime.report_span_for_test(
            "db.query", trace_id, span_id, 42, 1, [("db.statement", "SELECT 1")]
        )
        self.assertTrue(self.runtime.flush_tracing_for_test(5))

        batch = self._receive()
        self.assertEqual(batch.process.serviceName, "test service")
        process_tags = {tag.key: tag.vStr for tag in batch.process.tags}
        self.assertEqual(process_tags["instance"], "master")

        (span,) = batch.spans
        self.assertEqual(span.operationName, "db.query")
        # Thrift's i64s are signed, so the high span ID arrives negative,
        # exactly as jaeger-client would send it.
        self.assertEqual(span.traceIdLow, 5)
        self.assertEqual(span.traceIdHigh, 1 << 36)
        self.assertEqual(span.spanId, span_id - (1 << 64))
        self.assertEqual(span.parentSpanId, 42)
        self.assertEqual(span.flags, 1)
        self.assertEqual(span.duration, 1000)
        self.assertGreater(span.startTime, 0)
        self.assertEqual(
            [(tag.key, tag.vType, tag.vStr) for tag in span.tags],
            [("db.statement", ttypes.TagType.STRING, "SELECT 1")],
        )

    def _spans_under_python_span(
        self, run: "Callable[[], None]"
    ) -> "tuple[jaeger_client.SpanContext, list[ttypes.Span]]":
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("python", tracer=self.tracer) as scope:
                run()
        self.assertTrue(self.runtime.flush_tracing_for_test(5))
        return _context(scope.span), self._receive().spans

    def test_task_span_has_the_python_parent(self) -> None:
        parent, spans = self._spans_under_python_span(
            lambda: span_in_task_for_test(self.runtime, "db.persist_events")
        )

        (span,) = spans
        self.assertEqual(span.operationName, "db.persist_events")
        self.assertEqual(span.traceIdLow, _signed(parent.trace_id))
        self.assertEqual(span.parentSpanId, _signed(parent.span_id))
        self.assertEqual(span.flags, parent.flags)
        self.assertEqual(
            [(tag.key, tag.vLong) for tag in span.tags], [("db.queued", 3)]
        )
        (log,) = span.logs
        self.assertEqual(
            [(field.key, field.vStr) for field in log.fields], [("message", "commit")]
        )

    def test_thread_span_has_the_python_parent(self) -> None:
        parent, spans = self._spans_under_python_span(
            lambda: span_on_thread_for_test("state.resolve")
        )

        (span,) = spans
        self.assertEqual(span.operationName, "state.resolve")
        self.assertEqual(span.traceIdLow, _signed(parent.trace_id))
        self.assertEqual(span.parentSpanId, _signed(parent.span_id))

    def test_unsampled_python_parent(self) -> None:
        unsampled = _make_tracer(sampled=False)
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("python", tracer=unsampled):
                span_in_task_for_test(self.runtime, "not sampled")
        # A sampled span after it, so there is something to wait for.
        with LoggingContext(name="ctx", server_name="test_server"):
            with start_active_span("python", tracer=self.tracer):
                span_in_task_for_test(self.runtime, "sampled")
        self.assertTrue(self.runtime.flush_tracing_for_test(5))

        self.assertEqual(
            [span.operationName for span in self._receive().spans], ["sampled"]
        )

    def test_metrics(self) -> None:
        for span_id in range(1, 4):
            self.runtime.report_span_for_test("op", 1, span_id, 0, 1, [])
        self.assertTrue(self.runtime.flush_tracing_for_test(5))
        self.assertEqual(len(self._receive().spans), 3)

        self.assertEqual(self.runtime.tracing_stats(), (0, 3, 1, 0, 0, 0, 0))

        metrics = {
            (metric.name, sample.labels.get("reason")): sample.value
            for metric in rust_tracing_collector.collect()
            for sample in metric.samples
            if sample.labels.get("server_name") == self.hs.hostname
        }
        self.assertEqual(metrics[("synapse_rust_tracing_spans_sent", None)], 3)
        self.assertEqual(metrics[("synapse_rust_tracing_batches_sent", None)], 1)
        self.assertEqual(
            metrics[("synapse_rust_tracing_spans_dropped", "queue_full")], 0
        )

    def test_shutdown_sends_queued_spans(self) -> None:
        self.runtime.report_span_for_test("op", 1, 2, 0, 1, [])
        self.runtime.shutdown_tracing_for_test()

        self.assertEqual(len(self._receive().spans), 1)

        # Spans reported after shutdown are dropped and counted.
        self.runtime.report_span_for_test("op", 1, 3, 0, 1, [])
        stats = self.runtime.tracing_stats()
        assert stats is not None
        self.assertEqual(stats[5], 1)
