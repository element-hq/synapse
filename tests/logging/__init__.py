#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019 The Matrix.org Foundation C.I.C.
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

from tests.unittest import TestCase


class LoggerCleanupMixin(TestCase):
    def get_logger(self, handler: logging.Handler) -> logging.Logger:
        """
        Attach a handler to a logger and add clean-ups to remove revert this.
        """
        # Create a logger and add the handler to it.
        logger = logging.getLogger(__name__)
        logger.addHandler(handler)

        # Ensure the logger actually logs something.
        logger.setLevel(logging.INFO)

        # Ensure the logger gets cleaned-up appropriately.
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, logging.NOTSET)

        return logger
