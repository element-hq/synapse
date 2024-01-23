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
import re
from typing import Union

from twisted.internet import address, task
from twisted.web.client import FileBodyProducer
from twisted.web.iweb import IRequest

from synapse.api.errors import SynapseError


class RequestTimedOutError(SynapseError):
    """Exception representing timeout of an outbound request"""

    def __init__(self, msg: str):
        super().__init__(504, msg)


ACCESS_TOKEN_RE = re.compile(r"(\?.*access(_|%5[Ff])token=)[^&]*(.*)$")
CLIENT_SECRET_RE = re.compile(r"(\?.*client(_|%5[Ff])secret=)[^&]*(.*)$")


def redact_uri(uri: str) -> str:
    """Strips sensitive information from the uri replaces with <redacted>"""
    uri = ACCESS_TOKEN_RE.sub(r"\1<redacted>\3", uri)
    return CLIENT_SECRET_RE.sub(r"\1<redacted>\3", uri)


class QuieterFileBodyProducer(FileBodyProducer):
    """Wrapper for FileBodyProducer that avoids CRITICAL errors when the connection drops.

    Workaround for https://github.com/matrix-org/synapse/issues/4003 /
    https://twistedmatrix.com/trac/ticket/6528
    """

    def stopProducing(self) -> None:
        try:
            FileBodyProducer.stopProducing(self)
        except task.TaskStopped:
            pass


def get_request_uri(request: IRequest) -> bytes:
    """Return the full URI that was requested by the client"""
    return b"%s://%s%s" % (
        b"https" if request.isSecure() else b"http",
        _get_requested_host(request),
        # despite its name, "request.uri" is only the path and query-string.
        request.uri,
    )


def _get_requested_host(request: IRequest) -> bytes:
    hostname = request.getHeader(b"host")
    if hostname:
        return hostname

    # no Host header, use the address/port that the request arrived on
    host: Union[address.IPv4Address, address.IPv6Address] = request.getHost()

    hostname = host.host.encode("ascii")

    if request.isSecure() and host.port == 443:
        # default port for https
        return hostname

    if not request.isSecure() and host.port == 80:
        # default port for http
        return hostname

    return b"%s:%i" % (
        hostname,
        host.port,
    )


def get_request_user_agent(request: IRequest, default: str = "") -> str:
    """Return the last User-Agent header, or the given default."""
    # There could be raw utf-8 bytes in the User-Agent header.

    # N.B. if you don't do this, the logger explodes cryptically
    # with maximum recursion trying to log errors about
    # the charset problem.
    # c.f. https://github.com/matrix-org/synapse/issues/3471

    h = request.getHeader(b"User-Agent")
    return h.decode("ascii", "replace") if h else default
