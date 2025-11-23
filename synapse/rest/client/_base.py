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

"""This module contains base REST classes for constructing client v1 servlets."""

import logging
import re
from typing import Any, Awaitable, Callable, Iterable, Pattern, TypeVar, cast

from synapse.api.errors import InteractiveAuthIncompleteError
from synapse.api.urls import CLIENT_API_PREFIX
from synapse.types import JsonDict, StrCollection

logger = logging.getLogger(__name__)


def client_patterns(
    path_regex: str,
    releases: StrCollection = ("r0", "v3"),
    unstable: bool = True,
    v1: bool = False,
) -> Iterable[Pattern]:
    """Creates a regex compiled client path with the correct client path
    prefix.

    Args:
        path_regex: The regex string to match. This should NOT have a ^
            as this will be prefixed.
        releases: An iterable of releases to include this endpoint under.
        unstable: If true, include this endpoint under the "unstable" prefix.
        v1: If true, include this endpoint under the "api/v1" prefix.
    Returns:
        An iterable of patterns.
    """
    versions = []

    if v1:
        versions.append("api/v1")
    versions.extend(releases)
    if unstable:
        versions.append("unstable")

    if len(versions) == 1:
        versions_str = versions[0]
    elif len(versions) > 1:
        versions_str = "(" + "|".join(versions) + ")"
    else:
        raise RuntimeError("Must have at least one version for a URL")

    return [re.compile("^" + CLIENT_API_PREFIX + "/" + versions_str + path_regex)]


def set_timeline_upper_limit(filter_json: JsonDict, filter_timeline_limit: int) -> None:
    """
    Enforces a maximum limit of a timeline query.

    Params:
        filter_json: The timeline query to modify.
        filter_timeline_limit: The maximum limit to allow, passing -1 will
            disable enforcing a maximum limit.
    """
    if filter_timeline_limit < 0:
        return  # no upper limits
    timeline = filter_json.get("room", {}).get("timeline", {})
    if "limit" in timeline:
        filter_json["room"]["timeline"]["limit"] = min(
            filter_json["room"]["timeline"]["limit"], filter_timeline_limit
        )


C = TypeVar("C", bound=Callable[..., Awaitable[tuple[int, JsonDict]]])


def interactive_auth_handler(orig: C) -> C:
    """Wraps an on_POST method to handle InteractiveAuthIncompleteErrors

    Takes a on_POST method which returns an Awaitable (errcode, body) response
    and adds exception handling to turn a InteractiveAuthIncompleteError into
    a 401 response.

    Normal usage is:

    @interactive_auth_handler
    async def on_POST(self, request):
        # ...
        await self.auth_handler.check_auth
    """

    async def wrapped(*args: Any, **kwargs: Any) -> tuple[int, JsonDict]:
        try:
            return await orig(*args, **kwargs)
        except InteractiveAuthIncompleteError as e:
            return 401, e.result

    return cast(C, wrapped)
