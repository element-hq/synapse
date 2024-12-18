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

"""
Log formatters that output terse JSON.
"""

import json
import logging

_encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"))

# The properties of a standard LogRecord that should be ignored when generating
# JSON logs.
_IGNORED_LOG_RECORD_ATTRIBUTES = {
    "args",
    "asctime",
    "created",
    "exc_info",
    # exc_text isn't a public attribute, but is used to cache the result of formatException.
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "log": record.getMessage(),
            "namespace": record.name,
            "level": record.levelname,
        }

        return self._format(record, event)

    def _format(self, record: logging.LogRecord, event: dict) -> str:
        # Add attributes specified via the extra keyword to the logged event.
        for key, value in record.__dict__.items():
            if key not in _IGNORED_LOG_RECORD_ATTRIBUTES:
                event[key] = value

        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            if exc_type:
                event["exc_type"] = f"{exc_type.__name__}"
                event["exc_value"] = f"{exc_value}"

        return _encoder.encode(event)


class TerseJsonFormatter(JsonFormatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "log": record.getMessage(),
            "namespace": record.name,
            "level": record.levelname,
            "time": round(record.created, 2),
        }

        return self._format(record, event)
