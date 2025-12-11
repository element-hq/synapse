import logging
import time
from logging import Handler, LogRecord
from logging.handlers import MemoryHandler
from threading import Thread
from typing import cast

from twisted.internet.interfaces import IReactorCore


class PeriodicallyFlushingMemoryHandler(MemoryHandler):
    """
    This is a subclass of MemoryHandler that additionally spawns a background
    thread to periodically flush the buffer.

    This prevents messages from being buffered for too long.

    Additionally, all messages will be immediately flushed if the reactor has
    not yet been started.
    """

    def __init__(
        self,
        capacity: int,
        flushLevel: int = logging.ERROR,
        target: Handler | None = None,
        flushOnClose: bool = True,
        period: float = 5.0,
        reactor: IReactorCore | None = None,
    ) -> None:
        """
        period: the period between automatic flushes

        reactor: if specified, a custom reactor to use. If not specifies,
            defaults to the globally-installed reactor.
            Log entries will be flushed immediately until this reactor has
            started.
        """
        super().__init__(capacity, flushLevel, target, flushOnClose)

        self._flush_period: float = period
        self._active: bool = True
        self._reactor_started = False

        self._flushing_thread: Thread = Thread(
            name="PeriodicallyFlushingMemoryHandler flushing thread",
            target=self._flush_periodically,
            daemon=True,
        )
        self._flushing_thread.start()

        def on_reactor_running() -> None:
            self._reactor_started = True

        reactor_to_use: IReactorCore
        if reactor is None:
            from twisted.internet import reactor as global_reactor

            reactor_to_use = cast(IReactorCore, global_reactor)
        else:
            reactor_to_use = reactor

        # Call our hook when the reactor start up
        #
        # type-ignore: Ideally, we'd use `Clock.call_when_running(...)`, but
        # `PeriodicallyFlushingMemoryHandler` is instantiated via Python logging
        # configuration, so it's not straightforward to pass in the homeserver's clock
        # (and we don't want to burden other peoples logging config with the details).
        #
        # The important reason why we want to use `Clock.call_when_running` is so that
        # the callback runs with a logcontext as we want to know which server the logs
        # came from. But since we don't log anything in the callback, it's safe to
        # ignore the lint here.
        reactor_to_use.callWhenRunning(on_reactor_running)  # type: ignore[prefer-synapse-clock-call-when-running]

    def shouldFlush(self, record: LogRecord) -> bool:
        """
        Before reactor start-up, log everything immediately.
        Otherwise, fall back to original behaviour of waiting for the buffer to fill.
        """

        if self._reactor_started:
            return super().shouldFlush(record)
        else:
            return True

    def _flush_periodically(self) -> None:
        """
        Whilst this handler is active, flush the handler periodically.
        """

        while self._active:
            # flush is thread-safe; it acquires and releases the lock internally
            self.flush()
            time.sleep(self._flush_period)

    def close(self) -> None:
        self._active = False
        super().close()
