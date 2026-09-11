#!/usr/bin/env python3
"""Probe concurrent PostgreSQL sequence reads in a disposable local cluster.

Requires psycopg2 and PostgreSQL's initdb/postgres on PATH. Run as a non-root user.
No existing database is contacted. Data and a private Unix socket live in /tmp;
TCP is disabled. The cluster is stopped and removed even if the workload fails.

Examples:
    python scripts-dev/repro_postgres_sequence_read.py --seconds 10
    python scripts-dev/repro_postgres_sequence_read.py --seconds 20 --client-reads
    python scripts-dev/repro_postgres_sequence_read.py --seconds 10 --read locked

A nonzero regression count means consecutive reads in ONE backend decreased.
Zero regressions do not disprove a timing-dependent race. This tests PostgreSQL,
not Synapse, and does not restart the database during the experiment.
"""

import argparse
import concurrent.futures
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import psycopg2

READ_QUERIES = {
    "plain": "SELECT last_value FROM race_seq",
    "locked": "SELECT pg_sequence_last_value('race_seq'::regclass)",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--readers", type=int, default=3)
    parser.add_argument("--read", choices=READ_QUERIES, default="plain")
    parser.add_argument("--client-reads", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds <= 0 or args.readers <= 0:
        parser.error(
            "--seconds must be finite and positive; --readers must be positive"
        )
    # These libpq defaults can override even an explicitly supplied socket host.
    for variable in ("PGHOSTADDR", "PGSERVICE"):
        os.environ.pop(variable, None)
    binaries = {name: shutil.which(name) for name in ("initdb", "postgres")}
    if not all(binaries.values()):
        parser.error("initdb and postgres must be on PATH")

    query = READ_QUERIES[args.read]
    with tempfile.TemporaryDirectory(prefix="pg-sequence-read-") as temporary:
        root = Path(temporary)
        data = root / "data"
        socket = root / "socket"
        socket.mkdir(mode=0o700)
        log_path = root / "postgres.log"
        process = None
        stop = threading.Event()

        def connect():
            connection = psycopg2.connect(
                host=str(socket),
                port=5432,
                dbname="postgres",
                user="sequence_repro",
                connect_timeout=5,
                options=(
                    "-c statement_timeout=10000 -c synchronous_commit=on "
                    "-c jit=off -c max_parallel_workers_per_gather=0"
                ),
            )
            connection.autocommit = True
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SHOW data_directory")
                    if Path(cursor.fetchone()[0]).resolve() != data.resolve():
                        raise RuntimeError(
                            "Refusing to use a database outside the temporary cluster"
                        )
            except BaseException:
                connection.close()
                raise
            return connection

        with log_path.open("wb") as log:
            try:
                subprocess.run(
                    [
                        binaries["initdb"],
                        "-D",
                        str(data),
                        "-U",
                        "sequence_repro",
                        "--auth=trust",
                        "--no-locale",
                        "--encoding=UTF8",
                        "--no-instructions",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                    timeout=30,
                )
                process = subprocess.Popen(
                    [
                        binaries["postgres"],
                        "-D",
                        str(data),
                        "-k",
                        str(socket),
                        "-h",
                        "",
                        "-p",
                        "5432",
                        "-c",
                        "fsync=on",
                        "-c",
                        "synchronous_commit=on",
                        "-c",
                        "full_page_writes=on",
                        "-c",
                        "shared_buffers=32MB",
                        "-c",
                        "dynamic_shared_memory_type=mmap",
                        "-c",
                        "max_parallel_workers=0",
                        "-c",
                        "autovacuum=off",
                        "-c",
                        "unix_socket_permissions=0700",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                startup_deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("PostgreSQL exited during startup")
                    try:
                        connection = connect()
                        break
                    except psycopg2.OperationalError:
                        if time.monotonic() >= startup_deadline:
                            raise
                        time.sleep(0.05)

                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT version(), current_setting('fsync'), "
                            "current_setting('synchronous_commit'), "
                            "current_setting('full_page_writes'), "
                            "current_setting('listen_addresses')"
                        )
                        print("SETTINGS", cursor.fetchone(), flush=True)
                        cursor.execute(
                            "CREATE SEQUENCE race_seq INCREMENT BY 1 CACHE 1 NO CYCLE; "
                            "CREATE TABLE allocations (id bigint PRIMARY KEY)"
                        )
                        body = """
DECLARE
    previous_value bigint;
    current_value bigint;
    i integer;
BEGIN
    reads := 0;
    drops := 0;
    max_drop := 0;
    examples := '[]'::jsonb;
    FOR i IN 1..iterations LOOP
        %s;
        reads := reads + 1;
        IF current_value < previous_value THEN
            drops := drops + 1;
            max_drop := GREATEST(max_drop, previous_value - current_value);
            IF jsonb_array_length(examples) < 4 THEN
                examples := examples || jsonb_build_array(jsonb_build_object(
                    'before', previous_value, 'after', current_value,
                    'drop', previous_value - current_value));
            END IF;
        END IF;
        previous_value := current_value;
    END LOOP;
    RETURN NEXT;
END
""" % {
                            "plain": "SELECT last_value INTO current_value FROM race_seq",
                            "locked": (
                                "SELECT pg_sequence_last_value('race_seq'::regclass) "
                                "INTO current_value"
                            ),
                        }[args.read]
                        cursor.execute(
                            "CREATE FUNCTION poll_sequence(iterations integer) "
                            "RETURNS TABLE(reads bigint, drops bigint, max_drop bigint, "
                            "examples jsonb) LANGUAGE plpgsql VOLATILE AS %s",
                            (body,),
                        )
                        cursor.execute(
                            "SELECT seqincrement, seqcache, seqcycle, relpersistence "
                            "FROM pg_sequence JOIN pg_class ON oid = seqrelid "
                            "WHERE seqrelid = 'race_seq'::regclass"
                        )
                        print("SEQUENCE_CONFIG", cursor.fetchone(), flush=True)
                finally:
                    connection.close()

                print("WORKLOAD", vars(args), flush=True)
                barrier = threading.Barrier(args.readers + 1)

                def worker(number):
                    connection = None
                    try:
                        connection = connect()
                        barrier.wait(timeout=10)
                        deadline = time.monotonic() + args.seconds
                        with connection.cursor() as cursor:
                            if number is None:
                                allocated = 0
                                while time.monotonic() < deadline and not stop.is_set():
                                    cursor.execute(
                                        "INSERT INTO allocations SELECT nextval('race_seq') "
                                        "FROM generate_series(1, 1000)"
                                    )
                                    allocated += cursor.rowcount
                                return {"committed_allocations": allocated}

                            result = {
                                "reader": number,
                                "reads": 0,
                                "drops": 0,
                                "max_drop": 0,
                                "examples": [],
                            }
                            previous = None
                            while time.monotonic() < deadline and not stop.is_set():
                                if args.client_reads:
                                    cursor.execute(query)
                                    value = cursor.fetchone()[0]
                                    result["reads"] += 1
                                    if (
                                        previous is not None
                                        and value is not None
                                        and value < previous
                                    ):
                                        result["drops"] += 1
                                        result["max_drop"] = max(
                                            result["max_drop"], previous - value
                                        )
                                        if len(result["examples"]) < 8:
                                            result["examples"].append(
                                                {
                                                    "before": previous,
                                                    "after": value,
                                                    "drop": previous - value,
                                                }
                                            )
                                    previous = value
                                else:
                                    cursor.execute("SELECT * FROM poll_sequence(20000)")
                                    reads, drops, max_drop, examples = cursor.fetchone()
                                    result["reads"] += reads
                                    result["drops"] += drops
                                    result["max_drop"] = max(
                                        result["max_drop"], max_drop
                                    )
                                    result["examples"].extend(
                                        examples[: 8 - len(result["examples"])]
                                    )
                            return result
                    except BaseException:
                        stop.set()
                        barrier.abort()
                        raise
                    finally:
                        if connection is not None:
                            connection.close()

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=args.readers + 1
                ) as pool:
                    try:
                        futures = [pool.submit(worker, None)]
                        futures.extend(
                            pool.submit(worker, i) for i in range(args.readers)
                        )
                        results = [future.result() for future in futures]
                    finally:
                        stop.set()
                        barrier.abort()
                print("RESULTS", json.dumps(results), flush=True)
                print("TOTAL_READS", sum(r["reads"] for r in results[1:]), flush=True)
                print("TOTAL_DROPS", sum(r["drops"] for r in results[1:]), flush=True)

                connection = connect()
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*), min(id), max(id) FROM allocations"
                        )
                        count, minimum, maximum = cursor.fetchone()
                        cursor.execute(
                            "SELECT last_value, log_cnt, is_called FROM race_seq"
                        )
                        last_value, log_count, is_called = cursor.fetchone()
                        print(
                            "FINAL_STATE",
                            {
                                "count": count,
                                "min": minimum,
                                "max": maximum,
                                "last_value": last_value,
                                "log_cnt": log_count,
                                "is_called": is_called,
                            },
                            flush=True,
                        )
                        # The primary key plus these bounds proves no holes or duplicates.
                        if not (
                            minimum == 1
                            and count
                            == maximum
                            == last_value
                            == results[0]["committed_allocations"]
                            and is_called
                        ):
                            raise RuntimeError(
                                "Allocation integrity check failed; see FINAL_STATE"
                            )
                        print(
                            "VERIFIED: every ID from 1 to max committed exactly once; "
                            "final sequence equals max.",
                            flush=True,
                        )
                finally:
                    connection.close()
            except BaseException:
                print(log_path.read_text(), flush=True)
                raise
            finally:
                stop.set()
                if process is not None and process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                print(
                    "CLUSTER_STOPPED",
                    process is None or process.poll() is not None,
                    flush=True,
                )
    print("TEMP_DIRECTORY_REMOVED", not root.exists(), flush=True)


if __name__ == "__main__":
    main()
