/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 *
 * Originally licensed under the Apache License, Version 2.0:
 * <http://www.apache.org/licenses/LICENSE-2.0>.
 *
 * Derived from `MatrixConnector` in `src/server_resolver.rs` of
 * <https://github.com/erikjohnston/matrix-hyper-federation-client>.
 *
 * [This file includes modifications made by Element Creations Ltd]
 */

//! The connector hyper's connection pool calls to open a connection.
//!
//! For a `matrix-federation://<host>[:port]` URI it resolves the server name
//! (SRV, see [`super::resolve`]), tries each server in turn, and wraps the
//! first connection that opens in TLS, verified against `<host>`. The pool
//! keys connections on the URI's scheme and authority, so it pools per
//! (delegated) server name as Python does. `https://` URIs (the
//! `.well-known` fetch) connect straight to the host.
//!
//! This replaces Python's `MatrixHostnameEndpoint`, `HostnameEndpoint` and
//! `HTTPConnectProxyEndpoint`.

use std::{
    collections::HashMap,
    future::Future,
    io,
    net::{IpAddr, SocketAddr},
    pin::Pin,
    sync::Arc,
    task::{Context, Poll},
    time::Duration,
};

use anyhow::{anyhow, bail, Context as _};
use futures::{stream::FuturesUnordered, StreamExt};
use http::Uri;
use hyper_util::{
    client::legacy::connect::{Connected, Connection},
    rt::TokioIo,
};
use log::{debug, info};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::TcpStream,
};
use tokio_rustls::TlsConnector;

use super::{
    policy::{IpPolicy, Proxy},
    resolve::{parse_ip_literal, Server, ServerResolver},
    tls::{server_name, TlsPolicy},
    Now,
};
use crate::federation::transport::TransportError;

/// The scheme of federation request URIs.
pub const MATRIX_FEDERATION_SCHEME: &str = "matrix-federation";

/// How long to wait for a TCP connection, as Twisted's `HostnameEndpoint`.
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);

/// How long to wait for one address before also trying the next, as
/// Twisted's `HostnameEndpoint` does.
const ATTEMPT_DELAY: Duration = Duration::from_millis(300);

/// The longest `CONNECT` response head we read from a proxy.
const MAX_PROXY_RESPONSE: usize = 16 * 1024;

pub(super) struct ConnectorInner {
    pub servers: ServerResolver,
    pub ip_policy: IpPolicy,
    pub tls: TlsPolicy,
    pub proxy: Option<Proxy>,
    pub now: Now,
    /// Test-only: connect to these ports in place of the ones resolution
    /// picks, so tests can serve "port 443" and "port 8448" from an
    /// unprivileged listener. Empty in production.
    pub port_overrides: HashMap<u16, u16>,
}

/// A `tower_service::Service<Uri>` for hyper's legacy client.
#[derive(Clone)]
pub struct FederationConnector {
    pub(super) inner: Arc<ConnectorInner>,
}

impl tower_service::Service<Uri> for FederationConnector {
    type Response = FederationStream;
    type Error = TransportError;
    type Future = Pin<Box<dyn Future<Output = Result<FederationStream, TransportError>> + Send>>;

    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, uri: Uri) -> Self::Future {
        let inner = self.inner.clone();
        Box::pin(async move { inner.connect(uri).await })
    }
}

impl ConnectorInner {
    async fn connect(&self, uri: Uri) -> Result<FederationStream, TransportError> {
        let host = uri
            .host()
            .ok_or_else(|| TransportError::InvalidRequest(anyhow!("URI {uri} has no host")))?;

        let servers = match uri.scheme_str() {
            Some(MATRIX_FEDERATION_SCHEME) => {
                let now_secs = (self.now)() / 1000;
                self.servers
                    .resolve_server(host, uri.port_u16(), now_secs)
                    .await?
            }
            Some("https") => vec![Server {
                host: host.to_owned(),
                port: uri.port_u16().unwrap_or(443),
            }],
            other => {
                return Err(TransportError::InvalidRequest(anyhow!(
                    "unsupported scheme {other:?} in {uri}"
                )))
            }
        };

        // We verify the certificate against the host in the URI: for
        // federation that is the server name or its `.well-known` delegate,
        // not the SRV target.
        let tls_name = server_name(host).map_err(TransportError::Tls)?;
        let tls_config = self.tls.config_for(host);

        let mut first_error = None;
        for server in servers {
            match self.connect_to(&server).await {
                Ok(tcp) => {
                    let connector = TlsConnector::from(tls_config.clone());
                    return match connector.connect(tls_name.clone(), tcp).await {
                        Ok(tls) => Ok(FederationStream::new(Box::new(tls))),
                        Err(err) => Err(TransportError::Tls(
                            anyhow!(err).context(format!("TLS handshake with {host}")),
                        )),
                    };
                }
                Err(err) => {
                    info!(
                        "Failed to connect to {}:{}: {err}",
                        server.host, server.port
                    );
                    first_error.get_or_insert(err);
                }
            }
        }

        // The first failure is probably the most interesting.
        Err(first_error.unwrap_or_else(|| {
            TransportError::Connect(anyhow!("no servers to connect to for {host}"))
        }))
    }

    /// Open a byte stream to one server: TCP, possibly tunnelled through
    /// the proxy.
    async fn connect_to(&self, server: &Server) -> Result<Box<dyn Io>, TransportError> {
        let port = self
            .port_overrides
            .get(&server.port)
            .copied()
            .unwrap_or(server.port);

        if let Some(proxy) = self.proxy.as_ref().filter(|p| p.applies_to(&server.host)) {
            debug!(
                "Connecting to {}:{} via proxy {}:{}",
                server.host, server.port, proxy.host, proxy.port
            );
            return self.connect_via_proxy(proxy, &server.host, port).await;
        }

        debug!("Connecting to {}:{}", server.host, server.port);
        let ips = self
            .ip_policy
            .resolve(self.servers.dns().as_ref(), &server.host)
            .await?;
        let tcp = connect_tcp(ips, port).await?;
        Ok(Box::new(tcp))
    }

    async fn connect_via_proxy(
        &self,
        proxy: &Proxy,
        host: &str,
        port: u16,
    ) -> Result<Box<dyn Io>, TransportError> {
        // The proxy itself is not subject to the IP range policy, as in
        // Python, which connects to it with the unfiltered reactor.
        let ips = match self.servers.dns().lookup_ip(&proxy.host).await {
            Ok(ips) => ips,
            Err(err) => {
                return Err(TransportError::Dns(anyhow!(
                    "resolving proxy {}: {err:?}",
                    proxy.host
                )))
            }
        };
        let tcp = connect_tcp(ips, proxy.port).await?;

        let mut stream: Box<dyn Io> = if proxy.tls {
            let name = server_name(&proxy.host).map_err(TransportError::Tls)?;
            let connector = TlsConnector::from(self.tls.config_for(&proxy.host));
            let tls = connector.connect(name, tcp).await.map_err(|err| {
                TransportError::Tls(anyhow!(err).context(format!("TLS to proxy {}", proxy.host)))
            })?;
            Box::new(tls)
        } else {
            Box::new(tcp)
        };

        proxy_connect(&mut stream, host, port, proxy.authorization.as_deref())
            .await
            .map_err(TransportError::Connect)?;

        Ok(stream)
    }
}

/// Connect to the first of `ips` that answers. Like Twisted's
/// `HostnameEndpoint`, a new attempt starts every [`ATTEMPT_DELAY`] (or as
/// soon as one fails) without abandoning the earlier ones.
async fn connect_tcp(ips: Vec<IpAddr>, port: u16) -> Result<TcpStream, TransportError> {
    let attempt = async move {
        let mut pending = ips.into_iter().map(|ip| SocketAddr::new(ip, port));
        let mut attempts = FuturesUnordered::new();
        let mut first_error: Option<io::Error> = None;

        let start = |attempts: &mut FuturesUnordered<_>, addr: SocketAddr| {
            attempts.push(async move { (addr, TcpStream::connect(addr).await) });
        };

        match pending.next() {
            Some(addr) => start(&mut attempts, addr),
            None => bail!("no addresses to connect to"),
        }

        loop {
            let delay = tokio::time::sleep(ATTEMPT_DELAY);
            tokio::select! {
                result = attempts.next() => match result {
                    Some((_, Ok(stream))) => return Ok(stream),
                    Some((addr, Err(err))) => {
                        debug!("Connection attempt to {addr} failed: {err}");
                        first_error.get_or_insert(err);
                        if let Some(addr) = pending.next() {
                            start(&mut attempts, addr);
                        } else if attempts.is_empty() {
                            let err = first_error.expect("set above");
                            return Err(anyhow!(err));
                        }
                    }
                    None => unreachable!("attempts is never empty here"),
                },
                _ = delay, if pending.len() > 0 => {
                    if let Some(addr) = pending.next() {
                        start(&mut attempts, addr);
                    }
                }
            }
        }
    };

    match tokio::time::timeout(CONNECT_TIMEOUT, attempt).await {
        Ok(Ok(stream)) => {
            let _ = stream.set_nodelay(true);
            Ok(stream)
        }
        Ok(Err(err)) => Err(TransportError::Connect(err)),
        Err(_) => Err(TransportError::Connect(anyhow!(
            "timed out after {CONNECT_TIMEOUT:?}"
        ))),
    }
}

/// Ask an HTTP proxy for a tunnel to `host:port`.
async fn proxy_connect<S: AsyncRead + AsyncWrite + Unpin + ?Sized>(
    stream: &mut S,
    host: &str,
    port: u16,
    authorization: Option<&str>,
) -> anyhow::Result<()> {
    // An IPv6 literal needs brackets in an authority.
    let authority = match parse_ip_literal(host) {
        Some(IpAddr::V6(_)) if !host.starts_with('[') => format!("[{host}]:{port}"),
        _ => format!("{host}:{port}"),
    };

    let mut request = format!("CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n");
    if let Some(authorization) = authorization {
        request.push_str(&format!("Proxy-Authorization: {authorization}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).await?;
    stream.flush().await?;

    // Read the response head byte by byte, so we never consume bytes that
    // belong to the tunnelled connection.
    let mut head = Vec::with_capacity(256);
    while !head.ends_with(b"\r\n\r\n") {
        if head.len() >= MAX_PROXY_RESPONSE {
            bail!("proxy response head too long");
        }
        let byte = stream
            .read_u8()
            .await
            .context("proxy closed the connection")?;
        head.push(byte);
    }

    let status_line = head
        .split(|&b| b == b'\n')
        .next()
        .map(|line| String::from_utf8_lossy(line).trim().to_owned())
        .unwrap_or_default();
    let status = status_line.split_whitespace().nth(1);
    if !status_line.starts_with("HTTP/1.") || status != Some("200") {
        bail!("proxy refused CONNECT to {authority}: {status_line}");
    }

    Ok(())
}

/// A byte stream the agent can speak TLS (or HTTP) over.
pub(super) trait Io: AsyncRead + AsyncWrite + Send + Unpin {}
impl<T: AsyncRead + AsyncWrite + Send + Unpin> Io for T {}

/// An open federation connection, as handed to hyper.
pub struct FederationStream {
    io: TokioIo<Box<dyn Io>>,
}

impl FederationStream {
    fn new(io: Box<dyn Io>) -> Self {
        FederationStream {
            io: TokioIo::new(io),
        }
    }
}

impl Connection for FederationStream {
    fn connected(&self) -> Connected {
        Connected::new()
    }
}

impl hyper::rt::Read for FederationStream {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_read(cx, buf)
    }
}

impl hyper::rt::Write for FederationStream {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        Pin::new(&mut self.io).poll_write(cx, buf)
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_flush(cx)
    }

    fn poll_shutdown(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.io).poll_shutdown(cx)
    }
}

#[cfg(test)]
mod tests {
    use tokio::io::duplex;

    use super::*;

    #[tokio::test]
    async fn proxy_connect_handshake() {
        let (mut client, mut proxy) = duplex(4096);

        let server = tokio::spawn(async move {
            let mut buf = vec![0u8; 1024];
            let n = proxy.read(&mut buf).await.unwrap();
            let request = String::from_utf8(buf[..n].to_vec()).unwrap();
            proxy
                .write_all(b"HTTP/1.1 200 Connection established\r\n\r\ntunnelled")
                .await
                .unwrap();
            request
        });

        proxy_connect(&mut client, "remote.example", 8448, Some("Basic eA=="))
            .await
            .unwrap();

        // The bytes after the response head are the tunnel's, untouched.
        let mut rest = [0u8; 9];
        client.read_exact(&mut rest).await.unwrap();
        assert_eq!(&rest, b"tunnelled");

        let request = server.await.unwrap();
        assert_eq!(
            request,
            "CONNECT remote.example:8448 HTTP/1.1\r\nHost: remote.example:8448\r\n\
             Proxy-Authorization: Basic eA==\r\n\r\n"
        );
    }

    #[tokio::test]
    async fn proxy_connect_refused() {
        let (mut client, mut proxy) = duplex(4096);
        tokio::spawn(async move {
            let mut buf = vec![0u8; 1024];
            let _ = proxy.read(&mut buf).await.unwrap();
            proxy
                .write_all(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                .await
                .unwrap();
        });

        let err = proxy_connect(&mut client, "::1", 8448, None)
            .await
            .unwrap_err();
        assert!(err.to_string().contains("403"), "{err}");
    }

    #[tokio::test]
    async fn tcp_tries_each_address() {
        // Nothing listens on port 1 of the first address, so the second wins.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let accept = tokio::spawn(async move { listener.accept().await.unwrap() });

        let stream = connect_tcp(
            vec!["127.0.0.2".parse().unwrap(), "127.0.0.1".parse().unwrap()],
            port,
        )
        .await
        .unwrap();
        assert_eq!(stream.peer_addr().unwrap().port(), port);
        accept.await.unwrap();

        let err = connect_tcp(vec![], port).await.unwrap_err();
        assert!(matches!(err, TransportError::Connect(_)), "{err}");
    }
}
