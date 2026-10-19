#!/usr/bin/env python3
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

"""Write the golden `emitBatch` message for the Rust thrift encoder's test.

Encodes a fixed batch with jaeger-client's generated thrift code and the
compact protocol, exactly as jaeger-client's UDP reporter does, and writes the
bytes to `rust/src/logging/opentracing/testdata/emit_batch.bin`. The test
`golden_emit_batch` in `rust/src/logging/opentracing/thrift.rs` builds the same
batch and compares byte for byte, so the two must change together.

Only rerun this when jaeger-client or thrift changes version, and update the
versions recorded in that test.

Usage: poetry run python scripts-dev/generate_jaeger_thrift_fixture.py
"""

import os
from importlib.metadata import version

from jaeger_client.thrift_gen.agent import Agent
from jaeger_client.thrift_gen.jaeger import ttypes
from thrift.protocol import TCompactProtocol  # type: ignore[import-untyped]
from thrift.transport import TTransport  # type: ignore[import-untyped]

OUTPUT = os.path.join(
    os.path.dirname(__file__),
    "..",
    "rust",
    "src",
    "logging",
    "opentracing",
    "testdata",
    "emit_batch.bin",
)


def signed(value: int) -> int:
    """jaeger-client's `id_to_int`: thrift i64 fields are signed."""
    return value - (1 << 64) if value >= (1 << 63) else value


def string_tag(key: str, value: str) -> ttypes.Tag:
    return ttypes.Tag(key=key, vType=ttypes.TagType.STRING, vStr=value)


def build_batch() -> ttypes.Batch:
    process = ttypes.Process(
        serviceName="test.server master",
        tags=[
            string_tag("hostname", "host-1"),
            string_tag("jaeger.version", "Python-4.8.0"),
        ],
    )

    trace_id = (0x0123456789ABCDEF << 64) | 0xFEDCBA9876543210

    every_type = [
        string_tag("db.statement", "SELECT 1"),
        ttypes.Tag(key="ratio", vType=ttypes.TagType.DOUBLE, vDouble=-1.5),
        ttypes.Tag(key="sampled", vType=ttypes.TagType.BOOL, vBool=True),
        ttypes.Tag(key="error", vType=ttypes.TagType.BOOL, vBool=False),
        ttypes.Tag(key="db.queued", vType=ttypes.TagType.LONG, vLong=-300),
        # The generated code writes `vBinary` with `writeString`, so it takes
        # a `str` and sends its UTF-8 bytes.
        ttypes.Tag(key="raw", vType=ttypes.TagType.BINARY, vBinary="raw-bytes"),
    ]

    full = ttypes.Span(
        traceIdLow=signed(trace_id & ((1 << 64) - 1)),
        traceIdHigh=signed(trace_id >> 64),
        spanId=signed(0x8000000000000001),
        parentSpanId=signed(0xFFFFFFFFFFFFFFFF),
        operationName="db.query",
        references=[
            ttypes.SpanRef(
                refType=ttypes.SpanRefType.FOLLOWS_FROM,
                traceIdLow=12345,
                traceIdHigh=0,
                spanId=67890,
            )
        ],
        flags=1,
        startTime=1_760_000_000_123_456,
        duration=789,
        tags=every_type,
        logs=[
            ttypes.Log(
                timestamp=1_760_000_000_123_999,
                fields=[string_tag("message", "commit")],
            )
        ],
    )

    # More than 14 tags, for the long list header.
    many_tags = ttypes.Span(
        traceIdLow=1,
        traceIdHigh=0,
        spanId=2,
        parentSpanId=0,
        operationName="many tags",
        references=None,
        flags=3,
        startTime=1_760_000_000_000_000,
        duration=0,
        tags=[string_tag(f"k{i}", "v" * i) for i in range(20)],
        logs=[],
    )

    # A minimal span, repeated so the batch's span list needs the long header.
    minimal = [
        ttypes.Span(
            traceIdLow=7,
            traceIdHigh=0,
            spanId=100 + i,
            parentSpanId=7,
            operationName="persist.write_chunk",
            references=None,
            flags=1,
            startTime=1_760_000_000_000_000 + i,
            duration=1_000_000 * i,
            tags=[],
            logs=[],
        )
        for i in range(15)
    ]

    return ttypes.Batch(process=process, spans=[full, many_tags, *minimal])


def main() -> None:
    buffer = TTransport.TMemoryBuffer()
    # `Agent.Client.__init__` starts a Tornado receive loop on the transport,
    # which a memory buffer does not have. Set up only what sending needs.
    client = Agent.Client.__new__(Agent.Client)
    client._transport = buffer
    client._oprot_factory = TCompactProtocol.TCompactProtocolFactory()
    client._seqid = 0
    # The client numbers its messages from 1, so this one has sequence ID 1.
    client.emitBatch(build_batch())

    data = buffer.getvalue()
    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "wb") as f:
        f.write(data)

    print(
        f"Wrote {len(data)} bytes to {os.path.normpath(OUTPUT)} with "
        f"jaeger-client {version('jaeger-client')}, thrift {version('thrift')}"
    )


if __name__ == "__main__":
    main()
