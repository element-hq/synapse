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
from io import StringIO
from types import TracebackType


class LogFormatter(logging.Formatter):
    """Log formatter which gives more detail for exceptions

    This is the same as the standard log formatter, except that when logging
    exceptions [typically via log.foo("msg", exc_info=1)], it prints the
    sequence that led up to the point at which the exception was caught.
    (Normally only stack frames between the point the exception was raised and
    where it was caught are logged).
    """

    def formatException(
        self,
        ei: tuple[
            type[BaseException] | None,
            BaseException | None,
            TracebackType | None,
        ],
    ) -> str:
        sio = StringIO()
        (typ, val, tb) = ei

        # log the stack above the exception capture point if possible, but
        # check that we actually have an f_back attribute to work around
        # https://twistedmatrix.com/trac/ticket/9305

        if tb and hasattr(tb.tb_frame, "f_back"):
            sio.write("Capture point (most recent call last):\n")
            traceback.print_stack(tb.tb_frame.f_back, None, sio)

        traceback.print_exception(typ, val, tb, None, sio)
        s = sio.getvalue()
        sio.close()
        if s[-1:] == "\n":
            s = s[:-1]
        return s
