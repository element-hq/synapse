#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
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
import logging
import traceback
from typing import Any, Coroutine, NoReturn, TypeVar

from parameterized import parameterized_class

from twisted.internet import defer
from twisted.internet.defer import CancelledError, Deferred, ensureDeferred
from twisted.python.failure import Failure

from synapse.logging.context import (
    SENTINEL_CONTEXT,
    LoggingContext,
    PreserveLoggingContext,
    current_context,
    make_deferred_yieldable,
)
from synapse.util.async_helpers import (
    AwakenableSleeper,
    ObservableDeferred,
    concurrently_execute,
    delay_cancellation,
    gather_optional_coroutines,
    stop_cancellation,
    timeout_deferred,
)

from tests.server import get_clock
from tests.unittest import TestCase, logcontext_clean

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ObservableDeferredTest(TestCase):
    def test_succeed(self) -> None:
        origin_d: "Deferred[int]" = Deferred()
        observable = ObservableDeferred(origin_d)

        observer1 = observable.observe()
        observer2 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)

        # check the first observer is called first
        def check_called_first(res: int) -> int:
            self.assertFalse(observer2.called)
            return res

        observer1.addBoth(check_called_first)

        # store the results
        results: list[int | None] = [None, None]

        def check_val(res: int, idx: int) -> int:
            results[idx] = res
            return res

        observer1.addCallback(check_val, 0)
        observer2.addCallback(check_val, 1)

        origin_d.callback(123)
        self.assertEqual(results[0], 123, "observer 1 callback result")
        self.assertEqual(results[1], 123, "observer 2 callback result")

    def test_failure(self) -> None:
        origin_d: Deferred = Deferred()
        observable = ObservableDeferred(origin_d, consumeErrors=True)

        observer1 = observable.observe()
        observer2 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)

        # check the first observer is called first
        def check_called_first(res: int) -> int:
            self.assertFalse(observer2.called)
            return res

        observer1.addBoth(check_called_first)

        # store the results
        results: list[Failure | None] = [None, None]

        def check_failure(res: Failure, idx: int) -> None:
            results[idx] = res
            return None

        observer1.addErrback(check_failure, 0)
        observer2.addErrback(check_failure, 1)

        try:
            raise Exception("gah!")
        except Exception as e:
            origin_d.errback(e)
        assert results[0] is not None
        self.assertEqual(str(results[0].value), "gah!", "observer 1 errback result")
        assert results[1] is not None
        self.assertEqual(str(results[1].value), "gah!", "observer 2 errback result")

    def test_cancellation(self) -> None:
        """Test that cancelling an observer does not affect other observers."""
        origin_d: "Deferred[int]" = Deferred()
        observable = ObservableDeferred(origin_d, consumeErrors=True)

        observer1 = observable.observe()
        observer2 = observable.observe()
        observer3 = observable.observe()

        self.assertFalse(observer1.called)
        self.assertFalse(observer2.called)
        self.assertFalse(observer3.called)

        # cancel the second observer
        observer2.cancel()
        self.assertFalse(observer1.called)
        self.failureResultOf(observer2, CancelledError)
        self.assertFalse(observer3.called)

        # other observers resolve as normal
        origin_d.callback(123)
        self.assertEqual(observer1.result, 123, "observer 1 callback result")
        self.assertEqual(observer3.result, 123, "observer 3 callback result")

        # additional observers resolve as normal
        observer4 = observable.observe()
        self.assertEqual(observer4.result, 123, "observer 4 callback result")


class TimeoutDeferredTest(TestCase):
    def setUp(self) -> None:
        self.reactor, self.clock = get_clock()

    def test_times_out(self) -> None:
        """Basic test case that checks that the original deferred is cancelled and that
        the timing-out deferred is errbacked
        """
        cancelled = False

        def canceller(_d: Deferred) -> None:
            nonlocal cancelled
            cancelled = True

        non_completing_d: Deferred = Deferred(canceller)
        timing_out_d = timeout_deferred(
            deferred=non_completing_d,
            timeout=1.0,
            clock=self.clock,
        )

        self.assertNoResult(timing_out_d)
        self.assertFalse(cancelled, "deferred was cancelled prematurely")

        self.reactor.pump((1.0,))

        self.assertTrue(cancelled, "deferred was not cancelled by timeout")
        self.failureResultOf(timing_out_d, defer.TimeoutError)

    def test_times_out_when_canceller_throws(self) -> None:
        """Test that we have successfully worked around
        https://twistedmatrix.com/trac/ticket/9534"""

        def canceller(_d: Deferred) -> None:
            raise Exception("can't cancel this deferred")

        non_completing_d: Deferred = Deferred(canceller)
        timing_out_d = timeout_deferred(
            deferred=non_completing_d,
            timeout=1.0,
            clock=self.clock,
        )

        self.assertNoResult(timing_out_d)

        self.reactor.pump((1.0,))

        self.failureResultOf(timing_out_d, defer.TimeoutError)

    @logcontext_clean
    async def test_logcontext_is_preserved_on_timeout_cancellation(self) -> None:
        """
        Test that the logcontext is preserved when we timeout and the deferred is
        cancelled.
        """
        # Sanity check that we start in the sentinel context
        self.assertEqual(current_context(), SENTINEL_CONTEXT)

        incomplete_deferred_was_cancelled = False

        def mark_was_cancelled(res: Failure) -> None:
            """
            A passthrough errback which sets `incomplete_deferred_was_cancelled`.

            This means we re-raise any exception and allows further errbacks (in
            `timeout_deferred(...)`) to do their thing. Just trying to be a transparent
            proxy of any exception while doing our internal test book-keeping.
            """
            nonlocal incomplete_deferred_was_cancelled
            if res.check(CancelledError):
                incomplete_deferred_was_cancelled = True
            else:
                logger.error(
                    "Expected incomplete_d to fail with `CancelledError` because our "
                    "`timeout_deferred(...)` utility canceled it but saw %s",
                    res,
                )

            # Re-raise the exception so that any further errbacks can do their thing as
            # normal
            res.raiseException()

        # Create a deferred which we will never complete
        incomplete_d: Deferred = Deferred()
        incomplete_d.addErrback(mark_was_cancelled)

        with LoggingContext(name="one", server_name="test_server") as context_one:
            timing_out_d = timeout_deferred(
                deferred=incomplete_d,
                timeout=1.0,
                clock=self.clock,
            )
            self.assertNoResult(timing_out_d)
            # We should still be in the logcontext we started in
            self.assertIs(current_context(), context_one)

            # Pump the reactor until we trigger the timeout
            #
            # We're manually pumping the reactor (and causing any pending callbacks to
            # be called) so we need to be in the sentinel logcontext to avoid leaking
            # our current logcontext into the reactor (which would then get picked up
            # and associated with the next thing the reactor does). `with
            # PreserveLoggingContext()` will reset the logcontext to the sentinel while
            # we're pumping the reactor in the block and return us back to our current
            # logcontext after the block.
            with PreserveLoggingContext():
                self.reactor.pump(
                    # We only need to pump `1.0` (seconds) as we set
                    # `timeout_deferred(timeout=1.0)` above
                    (1.0,)
                )

            # We expect the incomplete deferred to have been cancelled because of the
            # timeout by this point
            self.assertTrue(
                incomplete_deferred_was_cancelled,
                "incomplete deferred was not cancelled",
            )
            # We should see the `TimeoutError` (instead of a `CancelledError`)
            self.failureResultOf(timing_out_d, defer.TimeoutError)
            # We're still in the same logcontext
            self.assertIs(current_context(), context_one)

        # Back to the sentinel context
        self.assertEqual(current_context(), SENTINEL_CONTEXT)

    @logcontext_clean
    async def test_logcontext_is_not_lost_when_awaiting_on_timeout_cancellation(
        self,
    ) -> None:
        """
        Test that the logcontext isn't lost when we `await make_deferred_yieldable(...)`
        the deferred to complete/timeout and it times out.
        """

        # Sanity check that we start in the sentinel context
        self.assertEqual(current_context(), SENTINEL_CONTEXT)

        # Create a deferred which we will never complete
        incomplete_d: Deferred = Deferred()

        async def competing_task() -> None:
            with LoggingContext(
                name="competing", server_name="test_server"
            ) as context_competing:
                timing_out_d = timeout_deferred(
                    deferred=incomplete_d,
                    timeout=1.0,
                    clock=self.clock,
                )
                self.assertNoResult(timing_out_d)
                # We should still be in the logcontext we started in
                self.assertIs(current_context(), context_competing)

                # Mimic the normal use case to wait for the work to complete or timeout.
                #
                # In this specific test, we expect the deferred to timeout and raise an
                # exception at this point.
                await make_deferred_yieldable(timing_out_d)

                self.fail(
                    "We should not make it to this point as the `timing_out_d` should have been cancelled"
                )

        d = defer.ensureDeferred(competing_task())

        # Still in the sentinel context
        self.assertEqual(current_context(), SENTINEL_CONTEXT)

        # Pump until we trigger the timeout
        self.reactor.pump(
            # We only need to pump `1.0` (seconds) as we set
            # `timeout_deferred(timeout=1.0)` above
            (1.0,)
        )

        # Still in the sentinel context
        self.assertEqual(current_context(), SENTINEL_CONTEXT)

        # We expect a failure due to the timeout
        self.failureResultOf(d, defer.TimeoutError)

        # Back to the sentinel context at the end of the day
        self.assertEqual(current_context(), SENTINEL_CONTEXT)


class _TestException(Exception):  #
    pass


class ConcurrentlyExecuteTest(TestCase):
    def test_limits_runners(self) -> None:
        """If we have more tasks than runners, we should get the limit of runners"""
        started = 0
        waiters = []
        processed = []

        async def callback(v: int) -> None:
            # when we first enter, bump the start count
            nonlocal started
            started += 1

            # record the fact we got an item
            processed.append(v)

            # wait for the goahead before returning
            d2: "Deferred[int]" = Deferred()
            waiters.append(d2)
            await d2

        # set it going
        d2 = ensureDeferred(concurrently_execute(callback, [1, 2, 3, 4, 5], 3))

        # check we got exactly 3 processes
        self.assertEqual(started, 3)
        self.assertEqual(len(waiters), 3)

        # let one finish
        waiters.pop().callback(0)

        # ... which should start another
        self.assertEqual(started, 4)
        self.assertEqual(len(waiters), 3)

        # we still shouldn't be done
        self.assertNoResult(d2)

        # finish the job
        while waiters:
            waiters.pop().callback(0)

        # check everything got done
        self.assertEqual(started, 5)
        self.assertCountEqual(processed, [1, 2, 3, 4, 5])
        self.successResultOf(d2)

    def test_preserves_stacktraces(self) -> None:
        """Test that the stacktrace from an exception thrown in the callback is preserved"""
        d1: "Deferred[int]" = Deferred()

        async def callback(v: int) -> None:
            # alas, this doesn't work at all without an await here
            await d1
            raise _TestException("bah")

        async def caller() -> None:
            try:
                await concurrently_execute(callback, [1], 2)
            except _TestException as e:
                tb = traceback.extract_tb(e.__traceback__)
                # we expect to see "caller", "concurrently_execute" and "callback".
                self.assertEqual(tb[0].name, "caller")
                self.assertEqual(tb[1].name, "concurrently_execute")
                self.assertEqual(tb[-1].name, "callback")
            else:
                self.fail("No exception thrown")

        d2 = ensureDeferred(caller())
        d1.callback(0)
        self.successResultOf(d2)

    def test_preserves_stacktraces_on_preformed_failure(self) -> None:
        """Test that the stacktrace on a Failure returned by the callback is preserved"""
        d1: "Deferred[int]" = Deferred()
        f = Failure(_TestException("bah"))

        async def callback(v: int) -> None:
            # alas, this doesn't work at all without an await here
            await d1
            await defer.fail(f)

        async def caller() -> None:
            try:
                await concurrently_execute(callback, [1], 2)
            except _TestException as e:
                tb = traceback.extract_tb(e.__traceback__)

                # Remove twisted internals from the stack, as we don't care
                # about the precise details.
                tb = traceback.StackSummary(
                    t for t in tb if "/twisted/" not in t.filename
                )

                # we expect to see "caller", "concurrently_execute" at the top of the stack
                self.assertEqual(tb[0].name, "caller")
                self.assertEqual(tb[1].name, "concurrently_execute")
                # ... some stack frames from the implementation of `concurrently_execute` ...
                # and at the bottom of the stack we expect to see "callback"
                self.assertEqual(tb[-1].name, "callback")
            else:
                self.fail("No exception thrown")

        d2 = ensureDeferred(caller())
        d1.callback(0)
        self.successResultOf(d2)


@parameterized_class(
    ("wrapper",),
    [("stop_cancellation",), ("delay_cancellation",)],
)
class CancellationWrapperTests(TestCase):
    """Common tests for the `stop_cancellation` and `delay_cancellation` functions."""

    wrapper: str

    def wrap_deferred(self, deferred: "Deferred[str]") -> "Deferred[str]":
        if self.wrapper == "stop_cancellation":
            return stop_cancellation(deferred)
        elif self.wrapper == "delay_cancellation":
            return delay_cancellation(deferred)
        else:
            raise ValueError(f"Unsupported wrapper type: {self.wrapper}")

    def test_succeed(self) -> None:
        """Test that the new `Deferred` receives the result."""
        deferred: "Deferred[str]" = Deferred()
        wrapper_deferred = self.wrap_deferred(deferred)

        # Success should propagate through.
        deferred.callback("success")
        self.assertTrue(wrapper_deferred.called)
        self.assertEqual("success", self.successResultOf(wrapper_deferred))

    def test_failure(self) -> None:
        """Test that the new `Deferred` receives the `Failure`."""
        deferred: "Deferred[str]" = Deferred()
        wrapper_deferred = self.wrap_deferred(deferred)

        # Failure should propagate through.
        deferred.errback(ValueError("abc"))
        self.assertTrue(wrapper_deferred.called)
        self.failureResultOf(wrapper_deferred, ValueError)
        self.assertIsNone(deferred.result, "`Failure` was not consumed")


class StopCancellationTests(TestCase):
    """Tests for the `stop_cancellation` function."""

    def test_cancellation(self) -> None:
        """Test that cancellation of the new `Deferred` leaves the original running."""
        deferred: "Deferred[str]" = Deferred()
        wrapper_deferred = stop_cancellation(deferred)

        # Cancel the new `Deferred`.
        wrapper_deferred.cancel()
        self.assertTrue(wrapper_deferred.called)
        self.failureResultOf(wrapper_deferred, CancelledError)
        self.assertFalse(
            deferred.called, "Original `Deferred` was unexpectedly cancelled"
        )

        # Now make the original `Deferred` fail.
        # The `Failure` must be consumed, otherwise unwanted tracebacks will be printed
        # in logs.
        deferred.errback(ValueError("abc"))
        self.assertIsNone(deferred.result, "`Failure` was not consumed")


class DelayCancellationTests(TestCase):
    """Tests for the `delay_cancellation` function."""

    def test_deferred_cancellation(self) -> None:
        """Test that cancellation of the new `Deferred` waits for the original."""
        deferred: "Deferred[str]" = Deferred()
        wrapper_deferred = delay_cancellation(deferred)

        # Cancel the new `Deferred`.
        wrapper_deferred.cancel()
        self.assertNoResult(wrapper_deferred)
        self.assertFalse(
            deferred.called, "Original `Deferred` was unexpectedly cancelled"
        )

        # Now make the original `Deferred` fail.
        # The `Failure` must be consumed, otherwise unwanted tracebacks will be printed
        # in logs.
        deferred.errback(ValueError("abc"))
        self.assertIsNone(deferred.result, "`Failure` was not consumed")

        # Now that the original `Deferred` has failed, we should get a `CancelledError`.
        self.failureResultOf(wrapper_deferred, CancelledError)

    def test_coroutine_cancellation(self) -> None:
        """Test that cancellation of the new `Deferred` waits for the original."""
        blocking_deferred: "Deferred[None]" = Deferred()
        completion_deferred: "Deferred[None]" = Deferred()

        async def task() -> NoReturn:
            await blocking_deferred
            completion_deferred.callback(None)
            # Raise an exception. Twisted should consume it, otherwise unwanted
            # tracebacks will be printed in logs.
            raise ValueError("abc")

        wrapper_deferred = delay_cancellation(task())

        # Cancel the new `Deferred`.
        wrapper_deferred.cancel()
        self.assertNoResult(wrapper_deferred)
        self.assertFalse(
            blocking_deferred.called, "Cancellation was propagated too deep"
        )
        self.assertFalse(completion_deferred.called)

        # Unblock the task.
        blocking_deferred.callback(None)
        self.assertTrue(completion_deferred.called)

        # Now that the original coroutine has failed, we should get a `CancelledError`.
        self.failureResultOf(wrapper_deferred, CancelledError)

    def test_suppresses_second_cancellation(self) -> None:
        """Test that a second cancellation is suppressed.

        Identical to `test_cancellation` except the new `Deferred` is cancelled twice.
        """
        deferred: "Deferred[str]" = Deferred()
        wrapper_deferred = delay_cancellation(deferred)

        # Cancel the new `Deferred`, twice.
        wrapper_deferred.cancel()
        wrapper_deferred.cancel()
        self.assertNoResult(wrapper_deferred)
        self.assertFalse(
            deferred.called, "Original `Deferred` was unexpectedly cancelled"
        )

        # Now make the original `Deferred` fail.
        # The `Failure` must be consumed, otherwise unwanted tracebacks will be printed
        # in logs.
        deferred.errback(ValueError("abc"))
        self.assertIsNone(deferred.result, "`Failure` was not consumed")

        # Now that the original `Deferred` has failed, we should get a `CancelledError`.
        self.failureResultOf(wrapper_deferred, CancelledError)

    def test_propagates_cancelled_error(self) -> None:
        """Test that a `CancelledError` from the original `Deferred` gets propagated."""
        deferred: "Deferred[str]" = Deferred()
        wrapper_deferred = delay_cancellation(deferred)

        # Fail the original `Deferred` with a `CancelledError`.
        cancelled_error = CancelledError()
        deferred.errback(cancelled_error)

        # The new `Deferred` should fail with exactly the same `CancelledError`.
        self.assertTrue(wrapper_deferred.called)
        self.assertIs(cancelled_error, self.failureResultOf(wrapper_deferred).value)

    def test_preserves_logcontext(self) -> None:
        """Test that logging contexts are preserved."""
        blocking_d: "Deferred[None]" = Deferred()

        async def inner() -> None:
            await make_deferred_yieldable(blocking_d)

        async def outer() -> None:
            with LoggingContext(name="c", server_name="test_server") as c:
                try:
                    await delay_cancellation(inner())
                    self.fail("`CancelledError` was not raised")
                except CancelledError:
                    self.assertEqual(c, current_context())
                    # Succeed with no error, unless the logging context is wrong.

        # Run and block inside `inner()`.
        d = defer.ensureDeferred(outer())
        self.assertEqual(SENTINEL_CONTEXT, current_context())

        d.cancel()

        # Now unblock. `outer()` will consume the `CancelledError` and check the
        # logging context.
        blocking_d.callback(None)
        self.successResultOf(d)


class AwakenableSleeperTests(TestCase):
    "Tests AwakenableSleeper"

    def test_sleep(self) -> None:
        reactor, clock = get_clock()
        sleeper = AwakenableSleeper(clock)

        d = defer.ensureDeferred(sleeper.sleep("name", 1000))

        reactor.pump([0.0])
        self.assertFalse(d.called)

        reactor.advance(0.5)
        self.assertFalse(d.called)

        reactor.advance(0.6)
        self.assertTrue(d.called)

    def test_explicit_wake(self) -> None:
        reactor, clock = get_clock()
        sleeper = AwakenableSleeper(clock)

        d = defer.ensureDeferred(sleeper.sleep("name", 1000))

        reactor.pump([0.0])
        self.assertFalse(d.called)

        reactor.advance(0.5)
        self.assertFalse(d.called)

        sleeper.wake("name")
        self.assertTrue(d.called)

        reactor.advance(0.6)

    def test_multiple_sleepers_timeout(self) -> None:
        reactor, clock = get_clock()
        sleeper = AwakenableSleeper(clock)

        d1 = defer.ensureDeferred(sleeper.sleep("name", 1000))

        reactor.advance(0.6)
        self.assertFalse(d1.called)

        # Add another sleeper
        d2 = defer.ensureDeferred(sleeper.sleep("name", 1000))

        # Only the first sleep should time out now.
        reactor.advance(0.6)
        self.assertTrue(d1.called)
        self.assertFalse(d2.called)

        reactor.advance(0.6)
        self.assertTrue(d2.called)

    def test_multiple_sleepers_wake(self) -> None:
        reactor, clock = get_clock()
        sleeper = AwakenableSleeper(clock)

        d1 = defer.ensureDeferred(sleeper.sleep("name", 1000))

        reactor.advance(0.5)
        self.assertFalse(d1.called)

        # Add another sleeper
        d2 = defer.ensureDeferred(sleeper.sleep("name", 1000))

        # Neither should fire yet
        reactor.advance(0.3)
        self.assertFalse(d1.called)
        self.assertFalse(d2.called)

        # Explicitly waking both up works
        sleeper.wake("name")
        self.assertTrue(d1.called)
        self.assertTrue(d2.called)


class GatherCoroutineTests(TestCase):
    """Tests for `gather_optional_coroutines`"""

    def make_coroutine(self) -> tuple[Coroutine[Any, Any, T], "defer.Deferred[T]"]:
        """Returns a coroutine and a deferred that it is waiting on to resolve"""

        d: "defer.Deferred[T]" = defer.Deferred()

        async def inner() -> T:
            with PreserveLoggingContext():
                return await d

        return inner(), d

    def test_single(self) -> None:
        "Test passing in a single coroutine works"

        with LoggingContext(name="test_ctx", server_name="test_server") as text_ctx:
            deferred: "defer.Deferred[None]"
            coroutine, deferred = self.make_coroutine()

            gather_deferred = defer.ensureDeferred(
                gather_optional_coroutines(coroutine)
            )

            # We shouldn't have a result yet, and should be in the sentinel
            # context.
            self.assertNoResult(gather_deferred)
            self.assertEqual(current_context(), SENTINEL_CONTEXT)

            # Resolving the deferred will resolve the coroutine
            deferred.callback(None)

            # All coroutines have resolved, and so we should have the results
            result = self.successResultOf(gather_deferred)
            self.assertEqual(result, (None,))

            # We should be back in the normal context.
            self.assertEqual(current_context(), text_ctx)

    def test_multiple_resolve(self) -> None:
        "Test passing in multiple coroutine that all resolve works"

        with LoggingContext(name="test_ctx", server_name="test_server") as test_ctx:
            deferred1: "defer.Deferred[int]"
            coroutine1, deferred1 = self.make_coroutine()
            deferred2: "defer.Deferred[str]"
            coroutine2, deferred2 = self.make_coroutine()

            gather_deferred = defer.ensureDeferred(
                gather_optional_coroutines(coroutine1, coroutine2)
            )

            # We shouldn't have a result yet, and should be in the sentinel
            # context.
            self.assertNoResult(gather_deferred)
            self.assertEqual(current_context(), SENTINEL_CONTEXT)

            # Even if we resolve one of the coroutines, we shouldn't have a result
            # yet
            deferred2.callback("test")
            self.assertNoResult(gather_deferred)
            self.assertEqual(current_context(), SENTINEL_CONTEXT)

            deferred1.callback(1)

            # All coroutines have resolved, and so we should have the results
            result = self.successResultOf(gather_deferred)
            self.assertEqual(result, (1, "test"))

            # We should be back in the normal context.
            self.assertEqual(current_context(), test_ctx)

    def test_multiple_fail(self) -> None:
        "Test passing in multiple coroutine where one fails does the right thing"

        with LoggingContext(name="test_ctx", server_name="test_server") as test_ctx:
            deferred1: "defer.Deferred[int]"
            coroutine1, deferred1 = self.make_coroutine()
            deferred2: "defer.Deferred[str]"
            coroutine2, deferred2 = self.make_coroutine()

            gather_deferred = defer.ensureDeferred(
                gather_optional_coroutines(coroutine1, coroutine2)
            )

            # We shouldn't have a result yet, and should be in the sentinel
            # context.
            self.assertNoResult(gather_deferred)
            self.assertEqual(current_context(), SENTINEL_CONTEXT)

            # Throw an exception in one of the coroutines
            exc = Exception("test")
            deferred2.errback(exc)

            # Expect the gather deferred to immediately fail
            result_exc = self.failureResultOf(gather_deferred)
            self.assertEqual(result_exc.value, exc)

            # We should be back in the normal context.
            self.assertEqual(current_context(), test_ctx)
