#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2014-2016 OpenMarket Ltd
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
import collections.abc
from typing import Any

from immutabledict import immutabledict


def freeze(o: Any) -> Any:
    if isinstance(o, dict):
        return immutabledict({k: freeze(v) for k, v in o.items()})

    if isinstance(o, immutabledict):
        return o

    if isinstance(o, (bytes, str)):
        return o

    try:
        return tuple(freeze(i) for i in o)
    except TypeError:
        pass

    return o


def unfreeze(o: Any) -> Any:
    if isinstance(o, collections.abc.Mapping):
        return {k: unfreeze(v) for k, v in o.items()}

    if isinstance(o, (bytes, str)):
        return o

    try:
        return [unfreeze(i) for i in o]
    except TypeError:
        pass

    return o
