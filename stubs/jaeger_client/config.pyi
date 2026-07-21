from __future__ import annotations

from typing import Any

from opentracing import Span as OpenTracingSpan, Tracer as OpenTracingTracer

class SpanContext: ...

class Span(OpenTracingSpan):
    context: SpanContext
    start_time: float | None
    end_time: float | None

class Tracer(OpenTracingTracer):
    active_span: Span | None

class Sampler: ...

class ConstSampler(Sampler):
    def __init__(self, decision: bool) -> None: ...

class Config:
    sampler: Any

    def __init__(
        self,
        config: Any,
        service_name: str,
        scope_manager: Any,
        metrics_factory: Any = ...,
    ) -> None: ...
    def create_tracer(self, sampler: Any, reporter: Any = ...) -> Any: ...
    def initialize_tracer(self, io_loop: Any = ...) -> Tracer | None: ...
