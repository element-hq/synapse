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
from typing import Any, Callable, cast
from unittest import SkipTest

from twisted.internet.testing import MemoryReactor

from synapse.logging.context import LoggingContext
from synapse.logging.opentracing import _enable_rust_tracing, start_active_span
from synapse.logging.rust_tracing_metrics import rust_tracing_collector
from synapse.rest import admin
from synapse.rest.client import login, room, sync
from synapse.server import HomeServer
from synapse.storage.rust_pool import RustConnectionPool
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


class _AgentTestCase(HomeserverTestCase):
    """Tracing on, as `init_tracer` sets it up: a sampling Jaeger tracer as the
    global tracer, and Rust reporting to a UDP socket the test reads."""

    if jaeger_client is None:
        skip = "Requires jaeger_client"  # type: ignore[unreachable]

    def prepare(self, reactor: MemoryReactor, clock: Clock, hs: HomeServer) -> None:
        self.addCleanup(set_tracing_enabled_for_test, False)

        self.agent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.agent.close)
        self.agent.bind(("127.0.0.1", 0))
        port = self.agent.getsockname()[1]

        config = jaeger_client.config.Config(
            config={
                "local_agent": {"reporting_host": "127.0.0.1", "reporting_port": port}
            },
            service_name="test service",
            scope_manager=LogContextScopeManager(),
        )
        self.python_spans = jaeger_client.reporter.InMemoryReporter()
        tracer = config.create_tracer(
            sampler=ConstSampler(True), reporter=self.python_spans
        )
        self.prepare_before_tracing()

        # Synapse's own spans, the request's and the persist queue's, come
        # from the global tracer: make it this one, as `init_tracer` does.
        previous_tracer = opentracing.global_tracer()
        opentracing.set_global_tracer(tracer)
        self.addCleanup(opentracing.set_global_tracer, previous_tracer)

        _enable_rust_tracing(hs, config, tracer)
        self.runtime = hs.get_rust_runtime()
        self.addCleanup(self.runtime.shutdown_tracing_for_test)

    def prepare_before_tracing(self) -> None:
        """Set-up that should not be traced."""

    def _received_spans(self) -> "list[ttypes.Span]":
        """Every span sent so far."""
        self.assertTrue(self.runtime.flush_tracing_for_test(5))
        spans: list[ttypes.Span] = []
        self.agent.settimeout(0.2)
        while True:
            try:
                data, _ = self.agent.recvfrom(70_000)
            except TimeoutError:
                return spans
            spans.extend(_decode_emit_batch(data).spans)

    def _python_spans_by_id(self) -> "dict[int, jaeger_client.Span]":
        """The Python spans reported so far, by thrift's signed span ID."""
        return {_signed(span.span_id): span for span in self.python_spans.get_spans()}

    def _python_ancestors(
        self, span: "ttypes.Span", python_by_id: "dict[int, jaeger_client.Span]"
    ) -> list[str]:
        """The names of the Python spans above the Rust span `span`, nearest
        first."""
        ancestors = []
        python_span = python_by_id.get(span.parentSpanId)
        while python_span is not None:
            ancestors.append(python_span.operation_name)
            parent_id = python_span.parent_id
            python_span = python_by_id.get(_signed(parent_id)) if parent_id else None
        return ancestors


class PersistTracingTestCase(_AgentTestCase):
    """Events persisted by the Rust persister report the persister's steps,
    with the write transaction's `db.*` spans under them.

    Needs the native Rust Postgres driver:

        SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.logging.test_rust_tracing
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_event_persistence"] = (
            "full"
        )
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        persistence = hs.get_storage_controllers().persistence
        if persistence is None or persistence._rust_persister is None:
            raise SkipTest("needs the Rust persister")
        return hs

    def prepare_before_tracing(self) -> None:
        self.user = self.register_user("user", "pass")
        self.token = self.login("user", "pass")
        self.room_id = self.helper.create_room_as(self.user, tok=self.token)

    def test_persist_span_tree(self) -> None:
        self.helper.send(self.room_id, "hello", tok=self.token)

        spans = self._received_spans()
        by_id = {span.spanId: span for span in spans}

        def parent(span: "ttypes.Span") -> "ttypes.Span | None":
            return by_id.get(span.parentSpanId)

        def tags(span: "ttypes.Span") -> dict[str, Any]:
            return {
                tag.key: tag.vStr if tag.vType == ttypes.TagType.STRING else tag.vLong
                for tag in span.tags
            }

        def named(name: str) -> "list[ttypes.Span]":
            return [s for s in spans if s.operationName == name]

        # The Rust persist hangs off the Python span of the persist queue,
        # in the same trace as the request.
        python_by_id = self._python_spans_by_id()
        (batch_next, _done) = named("persist.batch_next")
        python_parent = python_by_id.get(batch_next.parentSpanId)
        assert python_parent is not None, (batch_next, list(python_by_id.values()))
        python_trace = _signed(python_parent.trace_id)
        # The active Python span may be one the persist holds open, such as
        # the lock it waits for: walk up to the persist queue's span.
        self.assertIn(
            "persist_event_batch", self._python_ancestors(batch_next, python_by_id)
        )
        for span in spans:
            if _in_subtree(span, batch_next, by_id):
                self.assertEqual(span.traceIdLow, python_trace, span)

        # persist.batch_next → persist.prepare_chunk and persist.write_chunk
        (prepare_chunk,) = named("persist.prepare_chunk")
        (write_chunk,) = named("persist.write_chunk")
        self.assertEqual(prepare_chunk.parentSpanId, batch_next.spanId)
        self.assertEqual(write_chunk.parentSpanId, batch_next.spanId)
        self.assertEqual(tags(write_chunk)["room_id"], self.room_id)
        for stage in ("persist.state_delta", "persist.chain_cover"):
            (stage_span,) = named(stage)
            self.assertEqual(stage_span.parentSpanId, prepare_chunk.spanId)

        # persist.write_chunk → db.persist_events → db.connection → db.txn
        (interaction,) = named("db.persist_events")
        self.assertEqual(interaction.parentSpanId, write_chunk.spanId)
        txns = []
        for txn in named("db.txn"):
            connection = parent(txn)
            if connection is not None and connection.parentSpanId == interaction.spanId:
                txns.append(txn)
        self.assertEqual(len(txns), 1)
        (txn,) = txns
        self.assertEqual(tags(txn)["db.txn_desc"], "persist_events")

        # The transaction's writes go out queued, with the statements that
        # read and with the COMMIT.
        queries = [s for s in named("db.query") if s.parentSpanId == txn.spanId]
        self.assertGreater(len(queries), 0)
        self.assertTrue(
            any(tags(query).get("db.queued", 0) > 0 for query in queries), queries
        )
        self.assertIn("COMMIT", [tags(query).get("db.statement") for query in queries])

        # The transaction's steps are children of the transaction.
        steps = {s.operationName for s in spans if s.parentSpanId == txn.spanId} - {
            "db.query",
            "db.rollback",
        }
        self.assertIn("persist.txn.read", steps)
        self.assertIn("persist.txn.store_events", steps)


def _in_subtree(
    span: "ttypes.Span", root: "ttypes.Span", by_id: "dict[int, ttypes.Span]"
) -> bool:
    while True:
        if span.spanId == root.spanId:
            return True
        if span.parentSpanId not in by_id:
            return False
        span = by_id[span.parentSpanId]


class SyncTracingTestCase(_AgentTestCase):
    """A `/sync` whose rooms Rust computes reports a span per room under the
    request's Python span.

    Needs the native Rust Postgres driver:

        SYNAPSE_TEST_RUST_DRIVER=1 POSTGRES=1 run-trial.sh tests.logging.test_rust_tracing
    """

    servlets = [
        admin.register_servlets,
        room.register_servlets,
        login.register_servlets,
        sync.register_servlets,
    ]

    def default_config(self) -> dict[str, Any]:
        config = super().default_config()
        config.setdefault("experimental_features", {})["rust_sync"] = "on"
        return config

    def make_homeserver(self, reactor: MemoryReactor, clock: Clock) -> HomeServer:
        hs = self.setup_test_homeserver()
        if not isinstance(
            hs.get_datastores().main.db_pool._db_pool, RustConnectionPool
        ):
            raise SkipTest("needs the native Rust Postgres driver")
        if hs.config.experimental.rust_sync != "on":
            raise SkipTest("SYNAPSE_TEST_RUST_SYNC overrides the config")
        return hs

    def prepare_before_tracing(self) -> None:
        self.user = self.register_user("user", "pass")
        self.token = self.login("user", "pass")
        self.rooms = [
            self.helper.create_room_as(self.user, tok=self.token) for _ in range(2)
        ]

    def test_sync_span_tree(self) -> None:
        channel = self.make_request("GET", "/sync", access_token=self.token)
        self.assertEqual(channel.code, 200, channel.json_body)

        spans = self._received_spans()

        def named(name: str) -> "list[ttypes.Span]":
            return [s for s in spans if s.operationName == name]

        # Under the request's Python span.
        python_by_id = self._python_spans_by_id()
        (changes,) = named("sync.room_changes")
        (entries,) = named("sync.room_entries")
        for span in (changes, entries):
            self.assertIn("SyncRestServlet", self._python_ancestors(span, python_by_id))

        rooms = named("sync.room")
        self.assertEqual(len(rooms), 2)
        room_ids = {
            next(t.vStr for t in room_span.tags if t.key == "room_id")
            for room_span in rooms
        }
        self.assertEqual(room_ids, set(self.rooms))
        for room_span in rooms:
            self.assertEqual(room_span.parentSpanId, entries.spanId)
            self.assertEqual(
                next(t.vStr for t in room_span.tags if t.key == "outcome"), "entry"
            )

        # The rooms' reads nest under them.
        room_span_ids = {room_span.spanId for room_span in rooms}
        self.assertTrue(
            any(
                s.operationName == "db.connection" and s.parentSpanId in room_span_ids
                for s in spans
            ),
            [s.operationName for s in spans],
        )

        (encode,) = named("sync.encode")
        self.assertEqual(
            next(t.vLong for t in encode.tags if t.key == "rooms"), 2, encode
        )
        # Encoded on the reactor, under the request's Python span.
        self.assertEqual(encode.traceIdLow, entries.traceIdLow)
        self.assertIn("SyncRestServlet", self._python_ancestors(encode, python_by_id))
