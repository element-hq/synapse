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

"""Utilities for dealing with jinja2 templates"""

import time
import urllib.parse
from typing import TYPE_CHECKING, Callable, Optional, Sequence, Union

import jinja2

if TYPE_CHECKING:
    from synapse.config.homeserver import HomeServerConfig


def build_jinja_env(
    template_search_directories: Sequence[str],
    config: "HomeServerConfig",
    autoescape: Union[bool, Callable[[Optional[str]], bool], None] = None,
) -> jinja2.Environment:
    """Set up a Jinja2 environment to load templates from the given search path

    The returned environment defines the following filters:
        - format_ts: formats timestamps as strings in the server's local timezone
             (XXX: why is that useful??)
        - mxc_to_http: converts mxc: uris to http URIs. Args are:
             (uri, width, height, resize_method="crop")

    and the following global variables:
        - server_name: matrix server name

    Args:
        template_search_directories: directories to search for templates

        config: homeserver config, for things like `server_name` and `public_baseurl`

        autoescape: whether template variables should be autoescaped. bool, or
           a function mapping from template name to bool. Defaults to escaping templates
           whose names end in .html, .xml or .htm.

    Returns:
        jinja environment
    """

    if autoescape is None:
        autoescape = jinja2.select_autoescape()

    loader = jinja2.FileSystemLoader(template_search_directories)
    env = jinja2.Environment(loader=loader, autoescape=autoescape)

    # Update the environment with our custom filters
    env.filters.update(
        {
            "format_ts": _format_ts_filter,
            "mxc_to_http": _create_mxc_to_http_filter(config.server.public_baseurl),
            "localpart_from_email": _localpart_from_email_filter,
        }
    )

    # common variables for all templates
    env.globals.update({"server_name": config.server.server_name})

    return env


def _create_mxc_to_http_filter(
    public_baseurl: Optional[str],
) -> Callable[[str, int, int, str], str]:
    """Create and return a jinja2 filter that converts MXC urls to HTTP

    Args:
        public_baseurl: The public, accessible base URL of the homeserver
    """

    def mxc_to_http_filter(
        value: str, width: int, height: int, resize_method: str = "crop"
    ) -> str:
        if not public_baseurl:
            raise RuntimeError(
                "public_baseurl must be set in the homeserver config to convert MXC URLs to HTTP URLs."
            )

        if value[0:6] != "mxc://":
            return ""

        server_and_media_id = value[6:]
        fragment = None
        if "#" in server_and_media_id:
            server_and_media_id, fragment = server_and_media_id.split("#", 1)
            fragment = "#" + fragment

        params = {"width": width, "height": height, "method": resize_method}
        return "%s_matrix/media/v1/thumbnail/%s?%s%s" % (
            public_baseurl,
            server_and_media_id,
            urllib.parse.urlencode(params),
            fragment or "",
        )

    return mxc_to_http_filter


def _format_ts_filter(value: int, format: str) -> str:
    return time.strftime(format, time.localtime(value / 1000))


def _localpart_from_email_filter(address: str) -> str:
    return address.rsplit("@", 1)[0]
