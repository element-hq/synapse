#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright (C) 2026 Element Creations Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#

import datetime
import os
import tempfile
from typing import cast
from unittest.mock import Mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from zope.interface import implementer

from OpenSSL import crypto
from twisted.internet.address import IPv4Address
from twisted.internet.interfaces import IHandshakeListener
from twisted.internet.protocol import Factory, Protocol
from twisted.internet.ssl import CertificateOptions
from twisted.internet.testing import MemoryReactorClock
from twisted.protocols.tls import TLSMemoryBIOFactory, TLSMemoryBIOProtocol

from synapse.app._base import refresh_certificate
from synapse.config._base import Config, RootConfig
from synapse.config.homeserver import HomeServerConfig
from synapse.config.tls import TlsConfig
from synapse.crypto.context_factory import ServerContextFactory

from tests.server import FakeTransport
from tests.unittest import TestCase


class FakeServer(Config):
    section = "server"

    def has_tls_listener(self) -> bool:
        return True


class TestConfig(RootConfig):
    config_classes = [FakeServer, TlsConfig]


def _write_self_signed_cert(cert_path: str, key_path: str, serial: int) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )


@implementer(IHandshakeListener)
class _HandshakeRecorder(Protocol):
    def __init__(self) -> None:
        self.handshake_completed = False

    def handshakeCompleted(self) -> None:
        self.handshake_completed = True


def _handshake(server_factory: TLSMemoryBIOFactory) -> crypto.X509:
    """Run one TLS handshake against `server_factory` in memory.

    Returns:
        The certificate the server presented to the client.
    """
    reactor = MemoryReactorClock()
    address = IPv4Address("TCP", "127.0.0.1", 8448)
    client_factory = TLSMemoryBIOFactory(
        CertificateOptions(verify=False),
        True,
        Factory.forProtocol(_HandshakeRecorder),
    )

    server = server_factory.buildProtocol(address)
    client = client_factory.buildProtocol(address)
    assert isinstance(server, TLSMemoryBIOProtocol)
    assert isinstance(client, TLSMemoryBIOProtocol)
    client_protocol = client.wrappedProtocol
    assert isinstance(client_protocol, _HandshakeRecorder)

    server.makeConnection(FakeTransport(client, reactor, server))
    client.makeConnection(FakeTransport(server, reactor, client))
    # Each `advance(0)` delivers the writes buffered on both transports. A TLS
    # handshake needs a few round trips; the assertion below catches a loop that
    # is too short.
    for _ in range(10):
        reactor.advance(0)

    assert client_protocol.handshake_completed, "TLS handshake did not complete"
    peer_certificate = client.getPeerCertificate()
    assert peer_certificate is not None

    client.loseConnection()
    server.loseConnection()
    for _ in range(10):
        reactor.advance(0)

    return peer_certificate


class RefreshCertificateTestCase(TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cert_path = os.path.join(tmp.name, "tls.crt")
        self.key_path = os.path.join(tmp.name, "tls.key")

    def _load_config(self) -> HomeServerConfig:
        t = TestConfig()
        t.tls.read_config(
            {
                "tls_certificate_path": self.cert_path,
                "tls_private_key_path": self.key_path,
            },
            config_dir_path="",
            data_dir_path="",
        )
        return cast(HomeServerConfig, t)

    def test_reload_serves_new_certificate_to_new_connections(self) -> None:
        """After `refresh_certificate`, new connections through the same listener
        are served the certificate now on disk."""
        _write_self_signed_cert(self.cert_path, self.key_path, serial=1)
        config = self._load_config()
        config.tls.read_certificate_from_disk()
        context_factory = ServerContextFactory(config)
        # The wrapper `reactor.listenSSL` puts around the context factory.
        server_factory = TLSMemoryBIOFactory(
            context_factory, False, Factory.forProtocol(Protocol)
        )
        hs = Mock()
        hs.config = config
        hs.tls_server_context_factory = context_factory
        self.assertEqual(_handshake(server_factory).get_serial_number(), 1)

        _write_self_signed_cert(self.cert_path, self.key_path, serial=2)
        refresh_certificate(hs)

        self.assertEqual(_handshake(server_factory).get_serial_number(), 2)

    def test_refresh_certificate_without_tls_listener_is_a_no_op(self) -> None:
        """Without a TLS listener, `refresh_certificate` does not read the
        certificate from disk."""
        hs = Mock()
        hs.tls_server_context_factory = None

        refresh_certificate(hs)

        hs.config.tls.read_certificate_from_disk.assert_not_called()
