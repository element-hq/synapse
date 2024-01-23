#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 Maxwell G <maxwell@gtmx.me>
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

from packaging.version import Version

try:
    from pydantic import __version__ as pydantic_version
except ImportError:
    import importlib.metadata

    pydantic_version = importlib.metadata.version("pydantic")

HAS_PYDANTIC_V2: bool = Version(pydantic_version).major == 2

__all__ = ("HAS_PYDANTIC_V2",)
