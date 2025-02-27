#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2020 The Matrix.org Foundation C.I.C.
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

from typing_extensions import Literal


class MetadataFilter(logging.Filter):
    """Logging filter that adds constant values to each record.

    Args:
        metadata: Key-value pairs to add to each record.
    """

    def __init__(self, metadata: dict):
        self._metadata = metadata

    def filter(self, record: logging.LogRecord) -> Literal[True]:
        for key, value in self._metadata.items():
            setattr(record, key, value)
        return True
