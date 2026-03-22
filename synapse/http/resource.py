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

"""Pure-Python replacement for twisted.web.resource.Resource.

Provides the putChild/getChild tree structure for URL routing without
any dependency on Twisted.
"""

from typing import Any


# Type alias for backward compatibility with code that references IResource.
IResource = Any


class Resource:
    """Pure-Python replacement for twisted.web.resource.Resource.

    Provides the putChild/getChild tree structure for URL routing.
    No networking, no Twisted -- just a tree data structure.
    """

    isLeaf: bool = False

    def __init__(self) -> None:
        self.children: dict[bytes, "Resource"] = {}

    def putChild(self, path: bytes, child: "Resource") -> None:
        self.children[path] = child

    def listNames(self) -> list[bytes]:
        return list(self.children.keys())

    def getChild(self, path: bytes, request: Any) -> "Resource":
        return self.children.get(path, self)

    def getChildWithDefault(self, path: bytes, request: Any) -> "Resource":
        if path in self.children:
            return self.children[path]
        return self.getChild(path, request)

    def render(self, request: Any) -> Any:
        pass
