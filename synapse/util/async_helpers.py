#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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

import abc
import asyncio
import collections
import inspect
import itertools
import logging
import typing
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncContextManager,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Coroutine,
    Dict,
    Generator,
    Generic,
    Hashable,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
    cast,
    overload,
)

import attr
from typing_extensions import Concatenate, Literal, ParamSpec

from twisted.internet import defer
from twisted.internet.defer import CancelledError
from twisted.internet.interfaces import IReactorTime
from twisted.python.failure import Failure

from synapse.logging.context import (
    PreserveLoggingContext,
    make_deferred_yieldable,
    run_in_background,
)
from synapse.util import Clock

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class AbstractObservableDeferred(Generic[_T], metaclass=abc.ABCMeta):
    """Abstract base class defining the consumer interface of ObservableDeferred"""

    __slots__ = ()

    @abc.abstractmethod
    def observe(self) -> "defer.Deferred[_T]":
        """Add a new observer for this ObservableDeferred

        This returns a brand new deferred that is resolved when the underlying
        deferred is resolved. Interacting with the returned deferred does not
        effect the underlying deferred.

        Note that the returned Deferred doesn't follow the Synapse logcontext rules -
        you will probably want to `make_deferred_yieldable` it.
        """
        ...


class ObservableDeferred(Generic[_T], AbstractObservableDeferred[_T]):
    """Wraps a deferred object so that we can add observer deferreds. These
    observer deferreds do not affect the callback chain of the original
    deferred.

    If consumeErrors is true errors will be captured from the origin deferred.

    Cancelling or otherwise resolving an observer will not affect the original
    ObservableDeferred.

    NB that it does not attempt to do anything with logcontexts; in general
    you should probably make_deferred_yieldable the deferreds
    returned by `observe`, and ensure that the original deferred runs its
    callbacks in the sentinel logcontext.
    """

    __slots__ = ["_deferred", "_observers", "_result"]

    _deferred: "defer.Deferred[_T]"
    _observers: Union[List["defer.Deferred[_T]"], Tuple[()]]
    _result: Union[None, Tuple[Literal[True], _T], Tuple[Literal[False], Failure]]

    def __init__(self, deferred: "defer.Deferred[_T]", consumeErrors: bool = False):
        object.__setattr__(self, "_deferred", deferred)
        object.__setattr__(self, "_result", None)
        object.__setattr__(self, "_observers", [])

        def callback(r: _T) -> _T:
            object.__setattr__(self, "_result", (True, r))

            # once we have set _result, no more entries will be added to _observers,
            # so it's safe to replace it with the empty tuple.
            observers = self._observers
            object.__setattr__(self, "_observers", ())

            for observer in observers:
                try:
                    observer.callback(r)
                except Exception as e:
                    logger.exception(
                        "%r threw an exception on .callback(%r), ignoring...",
                        observer,
                        r,
                        exc_info=e,
                    )
            return r

        def errback(f: Failure) -> Optional[Failure]:
            object.__setattr__(self, "_result", (False, f))

            # once we have set _result, no more entries will be added to _observers,
            # so it's safe to replace it with the empty tuple.
            observers = self._observers
            object.__setattr__(self, "_observers", ())

            for observer in observers:
                # This is a little bit of magic to correctly propagate stack
                # traces when we `await` on one of the observer deferreds.
                f.value.__failure__ = f
                try:
                    observer.errback(f)
                except Exception as e:
                    logger.exception(
                        "%r threw an exception on .errback(%r), ignoring...",
                        observer,
                        f,
                        exc_info=e,
                    )

            if consumeErrors:
                return None
            else:
                return f

        deferred.addCallbacks(callback, errback)

    def observe(self) -> "defer.Deferred[_T]":
        """Observe the underlying deferred.

        This returns a brand new deferred that is resolved when the underlying
        deferred is resolved. Interacting with the returned deferred does not
        effect the underlying deferred.
        """
        if not self._result:
            assert isinstance(self._observers, list)
            d: "defer.Deferred[_T]" = defer.Deferred()
            self._observers.append(d)
            return d
        elif self._result[0]:
            return defer.succeed(self._result[1])
        else:
            return defer.fail(self._result[1])

    def observers(self) -> "Collection[defer.Deferred[_T]]":
        return self._observers

    def has_called(self) -> bool:
        return self._result is not None

    def has_succeeded(self) -> bool:
        return self._result is not None and self._result[0] is True

    def get_result(self) -> Union[_T, Failure]:
        if self._result is None:
            raise ValueError(f"{self!r} has no result yet")
        return self._result[1]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._deferred, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._deferred, name, value)

    def __repr__(self) -> str:
        return "<ObservableDeferred object at %s, result=%r, _deferred=%r>" % (
            id(self),
            self._result,
            self._deferred,
        )


T = TypeVar("T")


async def concurrently_execute(
    func: Callable[[T], Any],
    args: Iterable[T],
    limit: int,
    delay_cancellation: bool = False,
) -> None:
    """Executes the function with each argument concurrently while limiting
    the number of concurrent executions.

    Args:
        func: Function to execute, should return a deferred or coroutine.
        args: List of arguments to pass to func, each invocation of func
            gets a single argument.
        limit: Maximum number of conccurent executions.
        delay_cancellation: Whether to delay cancellation until after the invocations
            have finished.

    Returns:
        None, when all function invocations have finished. The return values
        from those functions are discarded.
    """
    it = iter(args)

    async def _concurrently_execute_inner(value: T) -> None:
        try:
            while True:
                await maybe_awaitable(func(value))
                value = next(it)
        except StopIteration:
            pass

    # We use `itertools.islice` to handle the case where the number of args is
    # less than the limit, avoiding needlessly spawning unnecessary background
    # tasks.
    if delay_cancellation:
        await yieldable_gather_results_delaying_cancellation(
            _concurrently_execute_inner,
            (value for value in itertools.islice(it, limit)),
        )
    else:
        await yieldable_gather_results(
            _concurrently_execute_inner,
            (value for value in itertools.islice(it, limit)),
        )


P = ParamSpec("P")
R = TypeVar("R")


async def yieldable_gather_results(
    func: Callable[Concatenate[T, P], Awaitable[R]],
    iter: Iterable[T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> List[R]:
    """Executes the function with each argument concurrently.

    Args:
        func: Function to execute that returns a Deferred
        iter: An iterable that yields items that get passed as the first
            argument to the function
        *args: Arguments to be passed to each call to func
        **kwargs: Keyword arguments to be passed to each call to func

    Returns
        A list containing the results of the function
    """
    try:
        return await make_deferred_yieldable(
            defer.gatherResults(
                [run_in_background(func, item, *args, **kwargs) for item in iter],
                consumeErrors=True,
            )
        )
    except defer.FirstError as dfe:
        # unwrap the error from defer.gatherResults.

        # The raised exception's traceback only includes func() etc if
        # the 'await' happens before the exception is thrown - ie if the failure
        # happens *asynchronously* - otherwise Twisted throws away the traceback as it
        # could be large.
        #
        # We could maybe reconstruct a fake traceback from Failure.frames. Or maybe
        # we could throw Twisted into the fires of Mordor.

        # suppress exception chaining, because the FirstError doesn't tell us anything
        # very interesting.
        assert isinstance(dfe.subFailure.value, BaseException)
        raise dfe.subFailure.value from None


async def yieldable_gather_results_delaying_cancellation(
    func: Callable[Concatenate[T, P], Awaitable[R]],
    iter: Iterable[T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> List[R]:
    """Executes the function with each argument concurrently.
    Cancellation is delayed until after all the results have been gathered.

    See `yieldable_gather_results`.

    Args:
        func: Function to execute that returns a Deferred
        iter: An iterable that yields items that get passed as the first
            argument to the function
        *args: Arguments to be passed to each call to func
        **kwargs: Keyword arguments to be passed to each call to func

    Returns
        A list containing the results of the function
    """
    try:
        return await make_deferred_yieldable(
            delay_cancellation(
                defer.gatherResults(
                    [run_in_background(func, item, *args, **kwargs) for item in iter],
                    consumeErrors=True,
                )
            )
        )
    except defer.FirstError as dfe:
        assert isinstance(dfe.subFailure.value, BaseException)
        raise dfe.subFailure.value from None


T1 = TypeVar("T1")
T2 = TypeVar("T2")
T3 = TypeVar("T3")
T4 = TypeVar("T4")


@overload
def gather_results(
    deferredList: Tuple[()], consumeErrors: bool = ...
) -> "defer.Deferred[Tuple[()]]": ...


@overload
def gather_results(
    deferredList: Tuple["defer.Deferred[T1]"],
    consumeErrors: bool = ...,
) -> "defer.Deferred[Tuple[T1]]": ...


@overload
def gather_results(
    deferredList: Tuple["defer.Deferred[T1]", "defer.Deferred[T2]"],
    consumeErrors: bool = ...,
) -> "defer.Deferred[Tuple[T1, T2]]": ...


@overload
def gather_results(
    deferredList: Tuple[
        "defer.Deferred[T1]", "defer.Deferred[T2]", "defer.Deferred[T3]"
    ],
    consumeErrors: bool = ...,
) -> "defer.Deferred[Tuple[T1, T2, T3]]": ...


@overload
def gather_results(
    deferredList: Tuple[
        "defer.Deferred[T1]",
        "defer.Deferred[T2]",
        "defer.Deferred[T3]",
        "defer.Deferred[T4]",
    ],
    consumeErrors: bool = ...,
) -> "defer.Deferred[Tuple[T1, T2, T3, T4]]": ...


def gather_results(  # type: ignore[misc]
    deferredList: Tuple["defer.Deferred[T1]", ...],
    consumeErrors: bool = False,
) -> "defer.Deferred[Tuple[T1, ...]]":
    """Combines a tuple of `Deferred`s into a single `Deferred`.

    Wraps `defer.gatherResults` to provide type annotations that support heterogenous
    lists of `Deferred`s.
    """
    # The `type: ignore[misc]` above suppresses
    # "Overloaded function implementation cannot produce return type of signature 1/2/3"
    deferred = defer.gatherResults(deferredList, consumeErrors=consumeErrors)
    return deferred.addCallback(tuple)


@attr.s(slots=True, auto_attribs=True)
class _LinearizerEntry:
    # The number of things executing.
    count: int
    # Deferreds for the things blocked from executing.
    deferreds: typing.OrderedDict["defer.Deferred[None]", Literal[1]]


class Linearizer:
    """Limits concurrent access to resources based on a key. Useful to ensure
    only a few things happen at a time on a given resource.

    Example:

        async with limiter.queue("test_key"):
            # do some work.

    """

    def __init__(
        self,
        name: Optional[str] = None,
        max_count: int = 1,
        clock: Optional[Clock] = None,
    ):
        """
        Args:
            max_count: The maximum number of concurrent accesses
        """
        if name is None:
            self.name: Union[str, int] = id(self)
        else:
            self.name = name

        if not clock:
            from twisted.internet import reactor

            clock = Clock(cast(IReactorTime, reactor))
        self._clock = clock
        self.max_count = max_count

        # key_to_defer is a map from the key to a _LinearizerEntry.
        self.key_to_defer: Dict[Hashable, _LinearizerEntry] = {}

    def is_queued(self, key: Hashable) -> bool:
        """Checks whether there is a process queued up waiting"""
        entry = self.key_to_defer.get(key)
        if not entry:
            # No entry so nothing is waiting.
            return False

        # There are waiting deferreds only in the OrderedDict of deferreds is
        # non-empty.
        return bool(entry.deferreds)

    def queue(self, key: Hashable) -> AsyncContextManager[None]:
        @asynccontextmanager
        async def _ctx_manager() -> AsyncIterator[None]:
            entry = await self._acquire_lock(key)
            try:
                yield
            finally:
                self._release_lock(key, entry)

        return _ctx_manager()

    async def _acquire_lock(self, key: Hashable) -> _LinearizerEntry:
        """Acquires a linearizer lock, waiting if necessary.

        Returns once we have secured the lock.
        """
        entry = self.key_to_defer.setdefault(
            key, _LinearizerEntry(0, collections.OrderedDict())
        )

        if entry.count < self.max_count:
            # The number of things executing is less than the maximum.
            logger.debug(
                "Acquired uncontended linearizer lock %r for key %r", self.name, key
            )
            entry.count += 1
            return entry

        # Otherwise, the number of things executing is at the maximum and we have to
        # add a deferred to the list of blocked items.
        # When one of the things currently executing finishes it will callback
        # this item so that it can continue executing.
        logger.debug("Waiting to acquire linearizer lock %r for key %r", self.name, key)

        new_defer: "defer.Deferred[None]" = make_deferred_yieldable(defer.Deferred())
        entry.deferreds[new_defer] = 1

        try:
            await new_defer
        except Exception as e:
            logger.info("defer %r got err %r", new_defer, e)
            if isinstance(e, CancelledError):
                logger.debug(
                    "Cancelling wait for linearizer lock %r for key %r",
                    self.name,
                    key,
                )
            else:
                logger.warning(
                    "Unexpected exception waiting for linearizer lock %r for key %r",
                    self.name,
                    key,
                )

            # we just have to take ourselves back out of the queue.
            del entry.deferreds[new_defer]
            raise

        logger.debug("Acquired linearizer lock %r for key %r", self.name, key)
        entry.count += 1

        # if the code holding the lock completes synchronously, then it
        # will recursively run the next claimant on the list. That can
        # relatively rapidly lead to stack exhaustion. This is essentially
        # the same problem as http://twistedmatrix.com/trac/ticket/9304.
        #
        # In order to break the cycle, we add a cheeky sleep(0) here to
        # ensure that we fall back to the reactor between each iteration.
        #
        # This needs to happen while we hold the lock. We could put it on the
        # exit path, but that would slow down the uncontended case.
        try:
            await self._clock.sleep(0)
        except CancelledError:
            self._release_lock(key, entry)
            raise

        return entry

    def _release_lock(self, key: Hashable, entry: _LinearizerEntry) -> None:
        """Releases a held linearizer lock."""
        logger.debug("Releasing linearizer lock %r for key %r", self.name, key)

        # We've finished executing so check if there are any things
        # blocked waiting to execute and start one of them
        entry.count -= 1

        if entry.deferreds:
            (next_def, _) = entry.deferreds.popitem(last=False)

            # we need to run the next thing in the sentinel context.
            with PreserveLoggingContext():
                next_def.callback(None)
        elif entry.count == 0:
            # We were the last thing for this key: remove it from the
            # map.
            del self.key_to_defer[key]


class ReadWriteLock:
    """An async read write lock.

    Example:

        async with read_write_lock.read("test_key"):
            # do some work
    """

    # IMPLEMENTATION NOTES
    #
    # We track the most recent queued reader and writer deferreds (which get
    # resolved when they release the lock).
    #
    # Read: We know its safe to acquire a read lock when the latest writer has
    # been resolved. The new reader is appended to the list of latest readers.
    #
    # Write: We know its safe to acquire the write lock when both the latest
    # writers and readers have been resolved. The new writer replaces the latest
    # writer.

    def __init__(self) -> None:
        # Latest readers queued
        self.key_to_current_readers: Dict[str, Set[defer.Deferred]] = {}

        # Latest writer queued
        self.key_to_current_writer: Dict[str, defer.Deferred] = {}

    def read(self, key: str) -> AsyncContextManager:
        @asynccontextmanager
        async def _ctx_manager() -> AsyncIterator[None]:
            new_defer: "defer.Deferred[None]" = defer.Deferred()

            curr_readers = self.key_to_current_readers.setdefault(key, set())
            curr_writer = self.key_to_current_writer.get(key, None)

            curr_readers.add(new_defer)

            try:
                # We wait for the latest writer to finish writing. We can safely ignore
                # any existing readers... as they're readers.
                # May raise a `CancelledError` if the `Deferred` wrapping us is
                # cancelled. The `Deferred` we are waiting on must not be cancelled,
                # since we do not own it.
                if curr_writer:
                    await make_deferred_yieldable(stop_cancellation(curr_writer))
                yield
            finally:
                with PreserveLoggingContext():
                    new_defer.callback(None)
                self.key_to_current_readers.get(key, set()).discard(new_defer)

        return _ctx_manager()

    def write(self, key: str) -> AsyncContextManager:
        @asynccontextmanager
        async def _ctx_manager() -> AsyncIterator[None]:
            new_defer: "defer.Deferred[None]" = defer.Deferred()

            curr_readers = self.key_to_current_readers.get(key, set())
            curr_writer = self.key_to_current_writer.get(key, None)

            # We wait on all latest readers and writer.
            to_wait_on = list(curr_readers)
            if curr_writer:
                to_wait_on.append(curr_writer)

            # We can clear the list of current readers since `new_defer` waits
            # for them to finish.
            curr_readers.clear()
            self.key_to_current_writer[key] = new_defer

            to_wait_on_defer = defer.gatherResults(to_wait_on)
            try:
                # Wait for all current readers and the latest writer to finish.
                # May raise a `CancelledError` immediately after the wait if the
                # `Deferred` wrapping us is cancelled. We must only release the lock
                # once we have acquired it, hence the use of `delay_cancellation`
                # rather than `stop_cancellation`.
                await make_deferred_yieldable(delay_cancellation(to_wait_on_defer))
                yield
            finally:
                # Release the lock.
                with PreserveLoggingContext():
                    new_defer.callback(None)
                # `self.key_to_current_writer[key]` may be missing if there was another
                # writer waiting for us and it completed entirely within the
                # `new_defer.callback()` call above.
                if self.key_to_current_writer.get(key) == new_defer:
                    self.key_to_current_writer.pop(key)

        return _ctx_manager()


def timeout_deferred(
    deferred: "defer.Deferred[_T]", timeout: float, reactor: IReactorTime
) -> "defer.Deferred[_T]":
    """The in built twisted `Deferred.addTimeout` fails to time out deferreds
    that have a canceller that throws exceptions. This method creates a new
    deferred that wraps and times out the given deferred, correctly handling
    the case where the given deferred's canceller throws.

    (See https://twistedmatrix.com/trac/ticket/9534)

    NOTE: Unlike `Deferred.addTimeout`, this function returns a new deferred.

    NOTE: the TimeoutError raised by the resultant deferred is
    twisted.internet.defer.TimeoutError, which is *different* to the built-in
    TimeoutError, as well as various other TimeoutErrors you might have imported.

    Args:
        deferred: The Deferred to potentially timeout.
        timeout: Timeout in seconds
        reactor: The twisted reactor to use


    Returns:
        A new Deferred, which will errback with defer.TimeoutError on timeout.
    """
    new_d: "defer.Deferred[_T]" = defer.Deferred()

    timed_out = [False]

    def time_it_out() -> None:
        timed_out[0] = True

        try:
            deferred.cancel()
        except Exception:  # if we throw any exception it'll break time outs
            logger.exception("Canceller failed during timeout")

        # the cancel() call should have set off a chain of errbacks which
        # will have errbacked new_d, but in case it hasn't, errback it now.

        if not new_d.called:
            new_d.errback(defer.TimeoutError("Timed out after %gs" % (timeout,)))

    delayed_call = reactor.callLater(timeout, time_it_out)

    def convert_cancelled(value: Failure) -> Failure:
        # if the original deferred was cancelled, and our timeout has fired, then
        # the reason it was cancelled was due to our timeout. Turn the CancelledError
        # into a TimeoutError.
        if timed_out[0] and value.check(CancelledError):
            raise defer.TimeoutError("Timed out after %gs" % (timeout,))
        return value

    deferred.addErrback(convert_cancelled)

    def cancel_timeout(result: _T) -> _T:
        # stop the pending call to cancel the deferred if it's been fired
        if delayed_call.active():
            delayed_call.cancel()
        return result

    deferred.addBoth(cancel_timeout)

    def success_cb(val: _T) -> None:
        if not new_d.called:
            new_d.callback(val)

    def failure_cb(val: Failure) -> None:
        if not new_d.called:
            new_d.errback(val)

    deferred.addCallbacks(success_cb, failure_cb)

    return new_d


@attr.s(slots=True, frozen=True, auto_attribs=True)
class DoneAwaitable(Awaitable[R]):
    """Simple awaitable that returns the provided value."""

    value: R

    def __await__(self) -> Generator[Any, None, R]:
        yield None
        return self.value


def maybe_awaitable(value: Union[Awaitable[R], R]) -> Awaitable[R]:
    """Convert a value to an awaitable if not already an awaitable."""
    if inspect.isawaitable(value):
        return value

    # For some reason mypy doesn't deduce that value is not Awaitable here, even though
    # inspect.isawaitable returns a TypeGuard.
    assert not isinstance(value, Awaitable)
    return DoneAwaitable(value)


def stop_cancellation(deferred: "defer.Deferred[T]") -> "defer.Deferred[T]":
    """Prevent a `Deferred` from being cancelled by wrapping it in another `Deferred`.

    Args:
        deferred: The `Deferred` to protect against cancellation. Must not follow the
            Synapse logcontext rules.

    Returns:
        A new `Deferred`, which will contain the result of the original `Deferred`.
        The new `Deferred` will not propagate cancellation through to the original.
        When cancelled, the new `Deferred` will fail with a `CancelledError`.

        The new `Deferred` will not follow the Synapse logcontext rules and should be
        wrapped with `make_deferred_yieldable`.
    """
    new_deferred: "defer.Deferred[T]" = defer.Deferred()
    deferred.chainDeferred(new_deferred)
    return new_deferred


@overload
def delay_cancellation(awaitable: "defer.Deferred[T]") -> "defer.Deferred[T]": ...


@overload
def delay_cancellation(awaitable: Coroutine[Any, Any, T]) -> "defer.Deferred[T]": ...


@overload
def delay_cancellation(awaitable: Awaitable[T]) -> Awaitable[T]: ...


def delay_cancellation(awaitable: Awaitable[T]) -> Awaitable[T]:
    """Delay cancellation of a coroutine or `Deferred` awaitable until it resolves.

    Has the same effect as `stop_cancellation`, but the returned `Deferred` will not
    resolve with a `CancelledError` until the original awaitable resolves.

    Args:
        deferred: The coroutine or `Deferred` to protect against cancellation. May
            optionally follow the Synapse logcontext rules.

    Returns:
        A new `Deferred`, which will contain the result of the original coroutine or
        `Deferred`. The new `Deferred` will not propagate cancellation through to the
        original coroutine or `Deferred`.

        When cancelled, the new `Deferred` will wait until the original coroutine or
        `Deferred` resolves before failing with a `CancelledError`.

        The new `Deferred` will follow the Synapse logcontext rules if `awaitable`
        follows the Synapse logcontext rules. Otherwise the new `Deferred` should be
        wrapped with `make_deferred_yieldable`.
    """

    # First, convert the awaitable into a `Deferred`.
    if isinstance(awaitable, defer.Deferred):
        deferred = awaitable
    elif asyncio.iscoroutine(awaitable):
        # Ideally we'd use `Deferred.fromCoroutine()` here, to save on redundant
        # type-checking, but we'd need Twisted >= 21.2.
        deferred = defer.ensureDeferred(awaitable)
    else:
        # We have no idea what to do with this awaitable.
        # We assume it's already resolved, such as `DoneAwaitable`s or `Future`s from
        # `make_awaitable`, and let the caller `await` it normally.
        return awaitable

    def handle_cancel(new_deferred: "defer.Deferred[T]") -> None:
        # before the new deferred is cancelled, we `pause` it to stop the cancellation
        # propagating. we then `unpause` it once the wrapped deferred completes, to
        # propagate the exception.
        new_deferred.pause()
        new_deferred.errback(Failure(CancelledError()))

        deferred.addBoth(lambda _: new_deferred.unpause())

    new_deferred: "defer.Deferred[T]" = defer.Deferred(handle_cancel)
    deferred.chainDeferred(new_deferred)
    return new_deferred


class AwakenableSleeper:
    """Allows explicitly waking up deferreds related to an entity that are
    currently sleeping.
    """

    def __init__(self, reactor: IReactorTime) -> None:
        self._streams: Dict[str, Set[defer.Deferred[None]]] = {}
        self._reactor = reactor

    def wake(self, name: str) -> None:
        """Wake everything related to `name` that is currently sleeping."""
        stream_set = self._streams.pop(name, set())
        for deferred in stream_set:
            try:
                with PreserveLoggingContext():
                    deferred.callback(None)
            except Exception:
                pass

    async def sleep(self, name: str, delay_ms: int) -> None:
        """Sleep for the given number of milliseconds, or return if the given
        `name` is explicitly woken up.
        """

        # Create a deferred that gets called in N seconds
        sleep_deferred: "defer.Deferred[None]" = defer.Deferred()
        call = self._reactor.callLater(delay_ms / 1000, sleep_deferred.callback, None)

        # Create a deferred that will get called if `wake` is called with
        # the same `name`.
        stream_set = self._streams.setdefault(name, set())
        notify_deferred: "defer.Deferred[None]" = defer.Deferred()
        stream_set.add(notify_deferred)

        try:
            # Wait for either the delay or for `wake` to be called.
            await make_deferred_yieldable(
                defer.DeferredList(
                    [sleep_deferred, notify_deferred],
                    fireOnOneCallback=True,
                    fireOnOneErrback=True,
                    consumeErrors=True,
                )
            )
        finally:
            # Clean up the state
            curr_stream_set = self._streams.get(name)
            if curr_stream_set is not None:
                curr_stream_set.discard(notify_deferred)
                if len(curr_stream_set) == 0:
                    self._streams.pop(name)

            # Cancel the sleep if we were woken up
            if call.active():
                call.cancel()


class DeferredEvent:
    """Like threading.Event but for async code"""

    def __init__(self, reactor: IReactorTime) -> None:
        self._reactor = reactor
        self._deferred: "defer.Deferred[None]" = defer.Deferred()

    def set(self) -> None:
        if not self._deferred.called:
            self._deferred.callback(None)

    def clear(self) -> None:
        if self._deferred.called:
            self._deferred = defer.Deferred()

    def is_set(self) -> bool:
        return self._deferred.called

    async def wait(self, timeout_seconds: float) -> bool:
        if self.is_set():
            return True

        # Create a deferred that gets called in N seconds
        sleep_deferred: "defer.Deferred[None]" = defer.Deferred()
        call = self._reactor.callLater(timeout_seconds, sleep_deferred.callback, None)

        try:
            await make_deferred_yieldable(
                defer.DeferredList(
                    [sleep_deferred, self._deferred],
                    fireOnOneCallback=True,
                    fireOnOneErrback=True,
                    consumeErrors=True,
                )
            )
        finally:
            # Cancel the sleep if we were woken up
            if call.active():
                call.cancel()

        return self.is_set()
