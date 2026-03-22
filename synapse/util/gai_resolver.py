# This module previously contained a copy of Twisted's GAIResolver.
# It has been replaced with a thin wrapper around asyncio's getaddrinfo.
#
# For backward compatibility, the GAIResolver class is still provided but
# now uses asyncio internally when Twisted is not available.

from socket import (
    AF_INET,
    AF_INET6,
    AF_UNSPEC,
    SOCK_DGRAM,
    SOCK_STREAM,
    AddressFamily,
    SocketKind,
    gaierror,
    getaddrinfo,
)
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    NoReturn,
    Sequence,
)

# Try Twisted first for backward compatibility
try:
    from zope.interface import implementer

    from twisted.internet.address import IPv4Address, IPv6Address
    from twisted.internet.interfaces import (
        IAddress,
        IHostnameResolver,
        IHostResolution,
        IReactorThreads,
        IResolutionReceiver,
    )
    from twisted.internet.threads import deferToThreadPool

    if TYPE_CHECKING:
        try:
            from twisted.python.runtime import platform
        except ImportError:
            platform = None  # type: ignore[assignment]

        if platform and platform.supportsThreads():
            try:
                from twisted.python.threadpool import ThreadPool
            except ImportError:
                ThreadPool = object  # type: ignore[misc, assignment]
        else:
            ThreadPool = object  # type: ignore[misc, assignment]


    @implementer(IHostResolution)
    class HostResolution:
        """
        The in-progress resolution of a given hostname.
        """

        def __init__(self, name: str):
            self.name = name

        def cancel(self) -> NoReturn:
            raise NotImplementedError()


    _any = frozenset([IPv4Address, IPv6Address])

    _typesToAF = {
        frozenset([IPv4Address]): AF_INET,
        frozenset([IPv6Address]): AF_INET6,
        _any: AF_UNSPEC,
    }

    _afToType = {
        AF_INET: IPv4Address,
        AF_INET6: IPv6Address,
    }

    _transportToSocket = {
        "TCP": SOCK_STREAM,
        "UDP": SOCK_DGRAM,
    }

    _socktypeToType = {
        SOCK_STREAM: "TCP",
        SOCK_DGRAM: "UDP",
    }

    _GETADDRINFO_RESULT = list[
        tuple[
            AddressFamily,
            SocketKind,
            int,
            str,
            tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes],
        ]
    ]


    @implementer(IHostnameResolver)
    class GAIResolver:
        """
        L{IHostnameResolver} implementation that resolves hostnames by calling
        L{getaddrinfo} in a thread.
        """

        def __init__(
            self,
            reactor: IReactorThreads,
            getThreadPool: Callable[[], "ThreadPool"] | None = None,
            getaddrinfo: Callable[[str, int, int, int], _GETADDRINFO_RESULT] = getaddrinfo,
        ):
            self._reactor = reactor
            self._getThreadPool = (
                reactor.getThreadPool if getThreadPool is None else getThreadPool
            )
            self._getaddrinfo = getaddrinfo

        def resolveHostName(
            self,
            resolutionReceiver: IResolutionReceiver,
            hostName: str,
            portNumber: int = 0,
            addressTypes: Sequence[type[IAddress]] | None = None,
            transportSemantics: str = "TCP",
        ) -> IHostResolution:
            pool = self._getThreadPool()
            addressFamily = _typesToAF[
                _any if addressTypes is None else frozenset(addressTypes)
            ]
            socketType = _transportToSocket[transportSemantics]

            def get() -> _GETADDRINFO_RESULT:
                try:
                    return self._getaddrinfo(
                        hostName, portNumber, addressFamily, socketType
                    )
                except gaierror:
                    return []

            d = deferToThreadPool(self._reactor, pool, get)
            resolution = HostResolution(hostName)
            resolutionReceiver.resolutionBegan(resolution)

            @d.addCallback
            def deliverResults(result: _GETADDRINFO_RESULT) -> None:
                for family, socktype, _proto, _cannoname, sockaddr in result:
                    addrType = _afToType[family]
                    resolutionReceiver.addressResolved(
                        addrType(_socktypeToType.get(socktype, "TCP"), *sockaddr)
                    )
                resolutionReceiver.resolutionComplete()

            return resolution

except ImportError:
    # Twisted is not available. Provide a no-op stub.
    class GAIResolver:  # type: ignore[no-redef]
        """Stub: Twisted is not installed. Use asyncio.get_event_loop().getaddrinfo() instead."""
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass
