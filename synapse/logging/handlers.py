import logging
import time
from logging import Handler, LogRecord
from logging.handlers import MemoryHandler
from threading import Thread
from typing import Any, Optional


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
        reactor: Any = None,
    ) -> None:
        """
        period: the period between automatic flushes

        reactor: legacy parameter, ignored. Kept for config compat.
        """
        super().__init__(capacity, flushLevel, target, flushOnClose)

        self._flush_period: float = period
        self._active: bool = True
        # In the asyncio world there is no delayed reactor start —
        # the event loop is running by the time log messages are emitted.
        self._reactor_started = True

        self._flushing_thread: Thread = Thread(
            name="PeriodicallyFlushingMemoryHandler flushing thread",
            target=self._flush_periodically,
            daemon=True,
        )
        self._flushing_thread.start()

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
