#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2021 The Matrix.org Foundation C.I.C.
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
import os
import sys
import tempfile
from unittest.mock import patch

from twisted.logger import Logger, LoggingFile, LogLevel

from synapse.config.logger import _load_logging_config

from tests.unittest import TestCase

LOGGER_NAME = "tests.config.test_logger.reload"

LOG_CONFIG = f"""
version: 1
handlers:
  err:
    class: logging.StreamHandler
  out:
    class: logging.StreamHandler
    stream: ext://sys.stdout
loggers:
  {LOGGER_NAME}:
    level: INFO
    handlers: [err, out]
    propagate: false
disable_existing_loggers: false
"""


class LoadLoggingConfigTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(self._remove_handlers)

    def _remove_handlers(self) -> None:
        logger = logging.getLogger(LOGGER_NAME)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)

    def _load(self, log_config: str) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = os.path.join(tmp_dir, "log.yaml")
            with open(path, "w") as f:
                f.write(log_config)
            _load_logging_config(path)

    def test_handlers_use_real_streams_when_stdio_is_redirected(self) -> None:
        redirected_stdout = LoggingFile(Logger(namespace="stdout"), LogLevel.info)
        redirected_stderr = LoggingFile(Logger(namespace="stderr"), LogLevel.error)

        with (
            patch.object(sys, "stdout", redirected_stdout),
            patch.object(sys, "stderr", redirected_stderr),
        ):
            self._load(LOG_CONFIG)

            self.assertIs(sys.stdout, redirected_stdout)
            self.assertIs(sys.stderr, redirected_stderr)

        streams = [
            getattr(handler, "stream", None)
            for handler in logging.getLogger(LOGGER_NAME).handlers
        ]
        self.assertEqual(len(streams), 2)
        self.assertIn(sys.__stderr__, streams)
        self.assertIn(sys.__stdout__, streams)

    def test_stdio_is_restored_when_loading_fails(self) -> None:
        redirected_stderr = LoggingFile(Logger(namespace="stderr"), LogLevel.error)

        with patch.object(sys, "stderr", redirected_stderr):
            with self.assertRaises(ValueError):
                self._load(
                    "version: 1\nhandlers:\n  bad:\n    class: no.such.Handler\n"
                )

            self.assertIs(sys.stderr, redirected_stderr)
