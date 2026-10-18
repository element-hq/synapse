#!/usr/bin/env python
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

"""A TCP proxy in front of Postgres that counts round trips and can add latency.

A round trip, or "turn", is a client message that follows a server message on
the same connection: the client had to wait for the server before sending it.
Pipelined messages count as one turn.

    poetry run python scripts-dev/pg_roundtrip_proxy.py --listen 15432 \\
        --target postgres:5432 --delay-ms 1 --stats /tmp/stats.json

Signals:
  - SIGUSR2 resets the counters and starts recording a log of turns.
  - SIGUSR1 writes the counters, and the log since the last reset, to the
    `--stats` file as JSON: `turns`, `per_connection` and `log`, a list of
    `[connection, summary]` where the summary is the protocol message types
    and the SQL of the turn's first statement.

Run Synapse against the proxy, reset before a request, dump after it. Adding a
delay and comparing the request's wall time with and without it shows how many
of the turns are on the request's critical path.
"""

import argparse
import asyncio
import json
import os
import signal
from dataclasses import dataclass, field


@dataclass
class Conn:
    """What the proxy knows about one client connection."""

    turns: int = 0
    # Whether the last message seen went from the client to the server.
    client_spoke_last: bool = False
    # Prepared statement names to their SQL, from the Parse messages seen.
    statements: dict[bytes, str] = field(default_factory=dict)
    # Client bytes not yet parsed: the start of a message split across reads.
    pending: bytes = b""
    # Whether the startup packet, which has no type byte, has been seen.
    started: bool = False


class Proxy:
    def __init__(self, target: tuple[str, int], delay: float, stats_path: str):
        self.target = target
        self.delay = delay
        self.stats_path = stats_path
        self.turns = 0
        self.conns: dict[int, Conn] = {}
        # The turns since the last reset, or None before the first reset.
        self.log: list[tuple[int, str]] | None = None

    def describe(self, data: bytes, conn: Conn) -> str:
        """The frontend message types completed by `data`, and the SQL of the
        first Parse, Bind (by statement name) or simple Query among them.

        Messages can be split across reads, so a partial one is kept for the
        next read."""
        buf = conn.pending + data
        kinds: list[str] = []
        sql = ""
        i = 0
        while not conn.started and i + 8 <= len(buf):
            # The startup packet, and an SSL or GSS encryption request before
            # it, are a length then a code, with no type byte.
            length = int.from_bytes(buf[i : i + 4], "big")
            if i + length > len(buf):
                break
            code = int.from_bytes(buf[i + 4 : i + 8], "big")
            if code not in (80877103, 80877104):
                conn.started = True
            i += length
        while conn.started and i + 5 <= len(buf):
            kind = chr(buf[i])
            length = int.from_bytes(buf[i + 1 : i + 5], "big")
            if i + 1 + length > len(buf):
                break
            body = buf[i + 5 : i + 1 + length]
            kinds.append(kind)
            parts = body.split(b"\0")
            if kind == "P" and len(parts) > 1:
                conn.statements[parts[0]] = parts[1].decode(errors="replace")
                sql = sql or conn.statements[parts[0]]
            elif kind == "B" and not sql and len(parts) > 1:
                sql = conn.statements.get(parts[1], "?")
            elif kind == "Q" and not sql:
                sql = parts[0].decode(errors="replace")
            i += 1 + length
        conn.pending = buf[i:]
        return "".join(kinds)[:16] + " " + " ".join(sql.split())[:120]

    async def pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        conn_id: int,
        from_client: bool,
    ) -> None:
        conn = self.conns[conn_id]
        try:
            while data := await reader.read(65536):
                if from_client:
                    # Parse every message, to learn the statement names.
                    # Counting must go on whatever the parser makes of it.
                    try:
                        summary = self.describe(data, conn)
                    except Exception:
                        conn.pending = b""
                        summary = "? unparsed"
                    if not conn.client_spoke_last:
                        self.turns += 1
                        conn.turns += 1
                        if self.log is not None:
                            self.log.append((conn_id, summary))
                        if self.delay:
                            await asyncio.sleep(self.delay)
                    conn.client_spoke_last = True
                else:
                    conn.client_spoke_last = False
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        try:
            server_reader, server_writer = await asyncio.open_connection(*self.target)
        except OSError:
            client_writer.close()
            raise
        conn_id = len(self.conns)
        self.conns[conn_id] = Conn()
        await asyncio.gather(
            self.pipe(client_reader, server_writer, conn_id, True),
            self.pipe(server_reader, client_writer, conn_id, False),
        )

    def dump(self) -> None:
        # Written to a temporary file and renamed, so a reader never sees it
        # half written.
        tmp_path = self.stats_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(
                {
                    "turns": self.turns,
                    "per_connection": {
                        conn_id: conn.turns
                        for conn_id, conn in self.conns.items()
                        if conn.turns
                    },
                    "log": self.log or [],
                },
                f,
            )
        os.replace(tmp_path, self.stats_path)

    def reset(self) -> None:
        self.turns = 0
        for conn in self.conns.values():
            conn.turns = 0
        self.log = []


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", type=int, required=True, help="local port")
    parser.add_argument("--target", required=True, help="Postgres HOST:PORT")
    parser.add_argument(
        "--delay-ms", type=float, default=0, help="latency to add to each turn"
    )
    parser.add_argument("--stats", required=True, help="file SIGUSR1 writes to")
    args = parser.parse_args()

    host, _, port = args.target.rpartition(":")
    proxy = Proxy((host, int(port)), args.delay_ms / 1000, args.stats)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGUSR1, proxy.dump)
    loop.add_signal_handler(signal.SIGUSR2, proxy.reset)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", args.listen)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
