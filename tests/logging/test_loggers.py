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
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        logger = self._create_explicitly_configured_logger()

        with self.assertNoLogs(logger=logger, level=logging.NOTSET):
            # XXX: We have to set this again because of a Python bug:
            # https://github.com/python/cpython/issues/136958 (feel free to remove once
            # that is resolved and we update to a newer Python version that includes the
            # fix)
            logger.setLevel(logging.NOTSET)

            logger.debug("debug message")
            logger.info("info message")
            logger.error("error message")

    def test_logs_when_explicitly_configured(self) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)

        logger = self._create_explicitly_configured_logger()

        with self.assertLogs(logger=logger, level=logging.DEBUG) as cm:
            logger.debug("debug message")
            logger.info("info message")
            logger.error("error message")

            self.assertIncludes(
                set(cm.output),
                {
                    "DEBUG:test:debug message",
                    "ERROR:test:error message",
                    "INFO:test:info message",
                },
                exact=True,
            )
