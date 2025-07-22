#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2025 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#
#
import logging

from synapse.logging.loggers import ExplicitlyConfiguredLogger

from tests.unittest import TestCase


class ExplicitlyConfiguredLoggerTestCase(TestCase):
    def _create_explicitly_configured_logger(self) -> logging.Logger:
        original_logger_class = logging.getLoggerClass()
        logging.setLoggerClass(ExplicitlyConfiguredLogger)
        logger = logging.getLogger("test")
        # Restore the original logger class
        logging.setLoggerClass(original_logger_class)

        return logger

    def test_no_logs_when_not_set(self) -> None:
        """
        Test to make sure that nothing is logged when the logger is *not* explicitly
        configured.
        """
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)

        logger = self._create_explicitly_configured_logger()

        with self.assertLogs(logger=logger, level=logging.NOTSET) as cm:
            # XXX: We have to set this again because of a Python bug:
            # https://github.com/python/cpython/issues/136958 (feel free to remove once
            # that is resolved and we update to a newer Python version that includes the
            # fix)
            logger.setLevel(logging.NOTSET)

            logger.debug("debug message")
            logger.info("info message")
            logger.warning("warning message")
            logger.error("error message")

            # Nothing should be logged since the logger is *not* explicitly configured
            #
            # FIXME: Remove this whole block once we update to Python 3.10 or later and
            # have access to `assertNoLogs` (replace `assertLogs` with `assertNoLogs`)
            self.assertIncludes(
                set(cm.output),
                set(),
                exact=True,
            )
            # Stub log message to avoid `assertLogs` failing since it expects at least
            # one log message to be logged.
            logger.setLevel(logging.INFO)
            logger.info("stub message so `assertLogs` doesn't fail")

    def test_logs_when_explicitly_configured(self) -> None:
        """
        Test to make sure that logs are emitted when the logger is explicitly configured.
        """
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        logger = self._create_explicitly_configured_logger()

        with self.assertLogs(logger=logger, level=logging.DEBUG) as cm:
            logger.debug("debug message")
            logger.info("info message")
            logger.warning("warning message")
            logger.error("error message")

            self.assertIncludes(
                set(cm.output),
                {
                    "DEBUG:test:debug message",
                    "INFO:test:info message",
                    "WARNING:test:warning message",
                    "ERROR:test:error message",
                },
                exact=True,
            )

    def test_is_enabled_for_not_set(self) -> None:
        """
        Test to make sure `logger.isEnabledFor(...)` returns False when the logger is
        not explicitly configured.
        """

        logger = self._create_explicitly_configured_logger()

        # Unset the logger (not configured)
        logger.setLevel(logging.NOTSET)

        # The logger shouldn't be enabled for any level
        self.assertFalse(logger.isEnabledFor(logging.DEBUG))
        self.assertFalse(logger.isEnabledFor(logging.INFO))
        self.assertFalse(logger.isEnabledFor(logging.WARNING))
        self.assertFalse(logger.isEnabledFor(logging.ERROR))

    def test_is_enabled_for_info(self) -> None:
        """
        Test to make sure `logger.isEnabledFor(...)` returns True any levels above the
        explicitly configured level.
        """

        logger = self._create_explicitly_configured_logger()

        # Explicitly configure the logger to `INFO` level
        logger.setLevel(logging.INFO)

        # The logger should be enabled for INFO and above once explicitly configured
        self.assertFalse(logger.isEnabledFor(logging.DEBUG))
        self.assertTrue(logger.isEnabledFor(logging.INFO))
        self.assertTrue(logger.isEnabledFor(logging.WARNING))
        self.assertTrue(logger.isEnabledFor(logging.ERROR))
