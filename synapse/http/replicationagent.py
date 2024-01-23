#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2023 The Matrix.org Foundation C.I.C.
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
from typing import Dict, Optional

from zope.interface import implementer

from twisted.internet import defer
from twisted.internet.endpoints import (
    HostnameEndpoint,
    UNIXClientEndpoint,
    wrapClientTLS,
)
from twisted.internet.interfaces import IStreamClientEndpoint
from twisted.python.failure import Failure
from twisted.web.client import URI, HTTPConnectionPool, _AgentBase
from twisted.web.error import SchemeNotSupported
from twisted.web.http_headers import Headers
from twisted.web.iweb import (
    IAgent,
    IAgentEndpointFactory,
    IBodyProducer,
    IPolicyForHTTPS,
    IResponse,
)

from synapse.config.workers import (
    InstanceLocationConfig,
    InstanceTcpLocationConfig,
    InstanceUnixLocationConfig,
)
from synapse.types import ISynapseReactor

logger = logging.getLogger(__name__)


@implementer(IAgentEndpointFactory)
class ReplicationEndpointFactory:
    """Connect to a given TCP or UNIX socket"""

    def __init__(
        self,
        reactor: ISynapseReactor,
        instance_map: Dict[str, InstanceLocationConfig],
        context_factory: IPolicyForHTTPS,
    ) -> None:
        self.reactor = reactor
        self.instance_map = instance_map
        self.context_factory = context_factory

    def endpointForURI(self, uri: URI) -> IStreamClientEndpoint:
        """
        This part of the factory decides what kind of endpoint is being connected to.

        Args:
            uri: The pre-parsed URI object containing all the uri data

        Returns: The correct client endpoint object
        """
        # The given URI has a special scheme and includes the worker name. The
        # actual connection details are pulled from the instance map.
        worker_name = uri.netloc.decode("utf-8")
        location_config = self.instance_map[worker_name]
        scheme = location_config.scheme()

        if isinstance(location_config, InstanceTcpLocationConfig):
            endpoint = HostnameEndpoint(
                self.reactor,
                location_config.host,
                location_config.port,
            )
            if scheme == "https":
                endpoint = wrapClientTLS(
                    # The 'port' argument below isn't actually used by the function
                    self.context_factory.creatorForNetloc(
                        location_config.host.encode("utf-8"),
                        location_config.port,
                    ),
                    endpoint,
                )
            return endpoint
        elif isinstance(location_config, InstanceUnixLocationConfig):
            return UNIXClientEndpoint(self.reactor, location_config.path)
        else:
            raise SchemeNotSupported(f"Unsupported scheme: {scheme}")


@implementer(IAgent)
class ReplicationAgent(_AgentBase):
    """
    Client for connecting to replication endpoints via HTTP and HTTPS.

    Much of this code is copied from Twisted's twisted.web.client.Agent.
    """

    def __init__(
        self,
        reactor: ISynapseReactor,
        instance_map: Dict[str, InstanceLocationConfig],
        contextFactory: IPolicyForHTTPS,
        connectTimeout: Optional[float] = None,
        bindAddress: Optional[bytes] = None,
        pool: Optional[HTTPConnectionPool] = None,
    ):
        """
        Create a ReplicationAgent.

        Args:
            reactor: A reactor for this Agent to place outgoing connections.
            contextFactory: A factory for TLS contexts, to control the
                verification parameters of OpenSSL.  The default is to use a
                BrowserLikePolicyForHTTPS, so unless you have special
                requirements you can leave this as-is.
            connectTimeout: The amount of time that this Agent will wait
                for the peer to accept a connection.
            bindAddress: The local address for client sockets to bind to.
            pool: An HTTPConnectionPool instance, or None, in which
                case a non-persistent HTTPConnectionPool instance will be
                created.
        """
        _AgentBase.__init__(self, reactor, pool)
        endpoint_factory = ReplicationEndpointFactory(
            reactor, instance_map, contextFactory
        )
        self._endpointFactory = endpoint_factory

    def request(
        self,
        method: bytes,
        uri: bytes,
        headers: Optional[Headers] = None,
        bodyProducer: Optional[IBodyProducer] = None,
    ) -> "defer.Deferred[IResponse]":
        """
        Issue a request to the server indicated by the given uri.

        An existing connection from the connection pool may be used or a new
        one may be created.

        Currently, HTTP, HTTPS and UNIX schemes are supported in uri.

        This is copied from twisted.web.client.Agent, except:

        * It uses a different pool key (combining the scheme with either host & port or
          socket path).
        * It does not call _ensureValidURI(...) as the strictness of IDNA2008 is not
          required when using a worker's name as a 'hostname' for Synapse HTTP
          Replication machinery. Specifically, this allows a range of ascii characters
          such as '+' and '_' in hostnames/worker's names.

        See: twisted.web.iweb.IAgent.request
        """
        parsedURI = URI.fromBytes(uri)
        try:
            endpoint = self._endpointFactory.endpointForURI(parsedURI)
        except SchemeNotSupported:
            return defer.fail(Failure())

        worker_name = parsedURI.netloc.decode("utf-8")
        key_scheme = self._endpointFactory.instance_map[worker_name].scheme()
        key_netloc = self._endpointFactory.instance_map[worker_name].netloc()
        # This sets the Pool key to be:
        #  (http(s), <host:port>) or (unix, <socket_path>)
        key = (key_scheme, key_netloc)

        # _requestWithEndpoint comes from _AgentBase class
        return self._requestWithEndpoint(
            key,
            endpoint,
            method,
            parsedURI,
            headers,
            bodyProducer,
            parsedURI.originForm,
        )
