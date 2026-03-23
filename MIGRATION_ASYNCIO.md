Asyncio Migration: Synapse Twisted → asyncio

Overview

This document describes the complete migration of Synapse's Matrix homeserver from Twisted to asyncio + aiohttp. The work was done incrementally across multiple sessions, starting from a codebase with ~500 files importing
    Twisted.

Architecture

Before

- Event loop: Twisted reactor (with asyncioreactor bridge to asyncio)
- HTTP server: Twisted Resource/Site/Request tree
- HTTP client: Twisted treq + Agent + IResponse
- Database: Twisted adbapi.ConnectionPool
- Caches: DeferredCache using Twisted Deferred
- Async primitives: Deferred, defer.ensureDeferred, defer.gatherResults
- Replication: txredisapi (Twisted Redis client)
- Tests: Twisted trial.unittest, MemoryReactorClock, nest_asyncio

After

- Event loop: Native asyncio (asyncio.run(), asyncio.get_event_loop())
- HTTP server: aiohttp.web with SynapseRequest compatibility shim
- HTTP client: aiohttp.ClientSession (NativeSimpleHttpClient)
- Database: NativeConnectionPool using asyncio.loop.run_in_executor()
- Caches: FutureCache using asyncio.Future
- Async primitives: asyncio.Future, asyncio.ensure_future, asyncio.gather
- Replication: redis.asyncio (redis-py async)
- Tests: unittest.IsolatedAsyncioTestCase, native async test methods

Key Components Migrated

1. Database Layer (synapse/storage/)

- NativeConnectionPool (synapse/storage/native_database.py): asyncio-native DB pool using ThreadPoolExecutor + loop.run_in_executor(). Thread-local connections for normal DBs, shared connection for :memory: SQLite.
Single-worker executor for shared connections to prevent deadlocks.
- DatabasePool (synapse/storage/database.py): Uses NativeConnectionPool instead of adbapi.ConnectionPool. Fresh in-memory DB copy per test via sqlite3.Connection.backup() for test isolation.

2. Cache Layer (synapse/util/caches/)

- FutureCache (synapse/util/caches/future_cache.py): asyncio-native replacement for DeferredCache. Uses asyncio.Future and ObservableFuture instead of Deferred/ObservableDeferred.
- ObservableFuture (synapse/util/async_helpers.py): Wraps asyncio.Future so multiple observers can await the same result. Includes callback()/errback() compat methods for code that previously used Deferred API.
- Cache descriptors (synapse/util/caches/descriptors.py): DeferredCacheDescriptor uses asyncio.ensure_future() instead of defer.maybeDeferred.

3. HTTP Server (synapse/http/)

- aiohttp_shim.py (NEW): Compatibility shim providing SynapseRequest (wraps aiohttp.web.Request), SynapseSite (data-only config), ShimRequestHeaders/ShimResponseHeaders, aiohttp_handler_factory (catch-all aiohttp
handler), _resolve_resource (resource tree traversal).
- resource.py (NEW): Pure-Python Resource base class replacing twisted.web.resource.Resource. Provides putChild/getChild/children tree structure.
- server.py: Removed Resource inheritance from _AsyncResource/JsonResource. Removed NOT_DONE_YET, _ByteProducer, failure.Failure. Response helpers write directly to request buffer. StaticResource serves files without
Twisted's File.
- site.py: Now a thin re-export layer from aiohttp_shim.py.

4. HTTP Client (synapse/http/)

- NativeSimpleHttpClient (synapse/http/native_client.py): aiohttp-based replacement for SimpleHttpClient. Supports IP blocklisting via custom DNS resolver, proxy support, lazy session creation. Aliased as SimpleHttpClient
    for drop-in replacement.
- NativeReplicationClient (synapse/http/native_client.py): Routes synapse-replication:// URIs to worker instances via TCP or UNIX sockets.
- Federation client (synapse/http/matrixfederationclient.py): Uses aiohttp session with SRV resolution and well-known delegation instead of Twisted MatrixFederationAgent.

5. Clock & Async Helpers (synapse/util/)

- NativeClock (synapse/util/clock.py): asyncio-native clock with fake time support. sleep() uses internal _pending_sleeps heap for deterministic testing. advance() fires pending sleeps. NativeLoopingCall uses asyncio
tasks. Clock is now an alias for NativeClock.
- async_helpers.py: timeout_deferred → asyncio.wait_for, delay_cancellation → asyncio.shield, gather_results → asyncio.gather, Linearizer/ReadWriteLock/AwakenableSleeper → asyncio-native implementations.

6. Logging Context (synapse/logging/context.py)

- Uses contextvars.ContextVar for current context tracking.
- run_in_background returns asyncio.Task for coroutines, resolved asyncio.Future for plain values, failed Future for exceptions.
- make_deferred_yieldable is async def — awaits any awaitable with logcontext preservation.
- logcontext_error has re-entrancy guard to prevent infinite loops when logging triggers context switches.

7. App Startup (synapse/app/)

- _base.py: listen_http() creates aiohttp.web.Application with the shim handler. start_reactor() uses asyncio event loop. Event loop created early in main() before homeserver setup.
- homeserver.py: Creates asyncio event loop before constructing homeserver. Server listens on aiohttp TCPSite/UnixSite.

8. Redis/Replication (synapse/replication/tcp/)

- redis.py: Replaced txredisapi with redis.asyncio. RedisSubscriber uses pubsub.listen() async iterator. RedisReplicationManager handles reconnection with asyncio tasks.
- external_cache.py: Uses redis.asyncio.Redis directly with native await.

9. Rust Extension (rust/src/)

- http_client.rs: Replaced twisted.internet.defer.Deferred with asyncio.Future. create_future() uses asyncio.get_event_loop().create_future() and loop.call_soon_threadsafe() to deliver results from Tokio threads.
- http.rs: Functions renamed in docs but kept same signatures — they work with any Python object that has content/uri/method/requestHeaders attributes (compatible with both the aiohttp shim and legacy Twisted).

10. Test Infrastructure (tests/)

- TestCase extends unittest.IsolatedAsyncioTestCase — each test gets its own event loop managed by the framework.
- asyncSetUp: Homeserver setup is async — await self.make_homeserver(...), await setup_test_homeserver(...).
- make_request: Now async def — dispatches request and awaits the handler task directly. No more await_result pump loop.
- get_success(d): Now async def — just await d. No more event loop pumping.
- register_user, login: Now async.
- FakeReactor (ThreadedMemoryReactorClock): Pure-asyncio, linked to NativeClock via set_clock(). advance() delegates to clock.
- DB isolation: Each test gets a fresh in-memory SQLite via sqlite3.Connection.backup().

Dependencies Removed

- Twisted: No longer a runtime dependency. All imports wrapped in try/except ImportError.
- treq: Removed entirely.
- txredisapi: Replaced with redis.asyncio (redis-py).
- matrix-synapse-ldap3: Disabled (depends on Twisted). LDAP loaded dynamically.
- nest_asyncio: No longer needed — IsolatedAsyncioTestCase handles async tests natively.

Dependencies Added

- aiohttp: HTTP server and client.
- redis (redis-py): Async Redis client for replication.

Remaining Twisted References (11 files, all guarded)

All wrapped in try/except ImportError with asyncio fallbacks:
- synapse/http/client.py — legacy utility stubs
- synapse/http/connectproxyclient.py — proxy credential classes
- synapse/http/server.py — StaticResource fallback
- synapse/http/__init__.py — QuieterFileBodyProducer stub
- synapse/app/_base.py — manhole listener
- synapse/app/complement_fork_starter.py — test forking
- synapse/crypto/context_factory.py — TLS factories
- synapse/replication/tcp/protocol.py — TCP replication protocol
- synapse/replication/tcp/redis.py — Redis protocol stubs
- synapse/replication/tcp/resource.py — replication server factory
- synapse/util/manhole.py — SSH debug console

Key Design Decisions

1. Compatibility shim over rewrite: The aiohttp_shim.py provides a SynapseRequest that presents the same API as Twisted's Request — 100+ REST endpoint files needed zero changes.
2. Resource tree preservation: The putChild/getChild tree structure was kept via a pure-Python Resource class, with _resolve_resource() for path traversal.
3. Fake time via NativeClock: Tests control time deterministically via clock.advance(). The FakeReactor.advance() delegates to NativeClock.advance() so both share the same time source.
4. IsolatedAsyncioTestCase: Each test gets its own event loop. Test methods are async def and use await directly. No nested run_until_complete, no nest_asyncio, no pump loops. The event loop drives ALL tasks naturally.
5. Single-worker executor for shared connections: In-memory SQLite test DBs use max_workers=1 to prevent concurrent access deadlocks on the shared connection.
6. Cookie handling in shim: request.cookies list is flushed to Set-Cookie response headers in finish(), matching Twisted's automatic behaviour.
7. Form body parsing: The shim's prepare_for_dispatch() merges form-encoded POST bodies into request.args, matching Twisted's automatic behaviour.

Known Issues / Future Work

- Session lifetime off-by-one: Token expiry check uses < not <=, so tests advancing exactly to the expiry boundary need +1 second.
- Ratelimit pause: Uses clock.sleep() (fake time) — tests need reactor.advance() to fire the sleep.
- Background process logging context: BackgroundProcessLoggingContext.start() can be called on finished contexts. Re-entrancy guard in logcontext_error prevents infinite loops.
- Production server TLS: Live TLS certificate rotation not supported with aiohttp (was a Twisted reactor feature). Use a reverse proxy.
- Manhole: Still requires Twisted's SSH libraries. Could be replaced with asyncssh.
- Complement test forking: complement_fork_starter.py still references Twisted reactor types for compatibility.
