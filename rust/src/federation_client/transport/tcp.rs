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
 */

use std::{io, net::SocketAddr};

use async_trait::async_trait;
use tokio::net::TcpStream;

use super::{BoxedIo, Transport};

/// TCP with `TCP_NODELAY` set where the socket allows it.
pub struct TcpTransport;

async fn connect_tcp(addr: SocketAddr) -> io::Result<TcpStream> {
    let stream = TcpStream::connect(addr).await?;
    if let Err(error) = stream.set_nodelay(true) {
        log::debug!("Couldn't set TCP_NODELAY on {addr}: {error}");
    }
    Ok(stream)
}

#[async_trait]
impl Transport for TcpTransport {
    async fn connect(&self, addr: SocketAddr) -> io::Result<BoxedIo> {
        Ok(Box::new(connect_tcp(addr).await?))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nodelay_is_set() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_io()
            .build()
            .unwrap();
        runtime.block_on(async {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let stream = connect_tcp(listener.local_addr().unwrap()).await.unwrap();
            assert!(stream.nodelay().unwrap());
        });
    }
}
