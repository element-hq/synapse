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
 */

//! Bulk loading through `COPY ... FROM STDIN` in text format. Each table
//! gets its own connection with a COPY in progress; rows are buffered and
//! sent in chunks. A COPY is one transaction, so a table is either fully
//! loaded or (on error) untouched.

use anyhow::{Context as _, Result};
use bytes::Bytes;
use deadpool_postgres::{Object, Pool};
use futures_util::SinkExt;
use tokio_postgres::CopyInSink;

const CHUNK: usize = 1 << 20;

pub struct Sink {
    table: &'static str,
    buf: Vec<u8>,
    rows: u64,
    inner: Option<(Object, std::pin::Pin<Box<CopyInSink<Bytes>>>)>,
}

/// A value for one column of a COPY row.
pub enum F<'a> {
    S(&'a str),
    I(i64),
    B(bool),
    Null,
    /// Raw bytes for a `bytea` column.
    Bytes(&'a [u8]),
}

impl Sink {
    pub async fn open(pool: &Pool, table: &'static str, columns: &str) -> Result<Self> {
        let client = pool.get().await?;
        let sql = format!("COPY {table} ({columns}) FROM STDIN");
        let sink = client
            .copy_in::<_, Bytes>(&sql)
            .await
            .with_context(|| format!("starting COPY into {table}"))?;
        Ok(Self {
            table,
            buf: Vec::with_capacity(CHUNK + 4096),
            rows: 0,
            inner: Some((client, Box::pin(sink))),
        })
    }

    pub async fn row(&mut self, fields: &[F<'_>]) -> Result<()> {
        for (i, f) in fields.iter().enumerate() {
            if i > 0 {
                self.buf.push(b'\t');
            }
            match f {
                F::S(s) => escape_into(&mut self.buf, s.as_bytes()),
                F::I(n) => self.buf.extend_from_slice(n.to_string().as_bytes()),
                F::B(b) => self.buf.push(if *b { b't' } else { b'f' }),
                F::Null => self.buf.extend_from_slice(b"\\N"),
                F::Bytes(b) => {
                    self.buf.extend_from_slice(b"\\\\x");
                    self.buf.extend_from_slice(hex::encode(b).as_bytes());
                }
            }
        }
        self.buf.push(b'\n');
        self.rows += 1;
        if self.buf.len() >= CHUNK {
            self.flush().await?;
        }
        Ok(())
    }

    async fn flush(&mut self) -> Result<()> {
        if self.buf.is_empty() {
            return Ok(());
        }
        let (_, sink) = self.inner.as_mut().expect("sink closed");
        let chunk = Bytes::from(std::mem::replace(
            &mut self.buf,
            Vec::with_capacity(CHUNK + 4096),
        ));
        sink.send(chunk)
            .await
            .with_context(|| format!("sending COPY data to {}", self.table))?;
        Ok(())
    }

    /// Finish the COPY and commit. Returns the row count.
    pub async fn close(mut self) -> Result<u64> {
        self.flush().await?;
        let (client, mut sink) = self.inner.take().expect("sink closed");
        sink.as_mut()
            .close()
            .await
            .with_context(|| format!("finishing COPY into {}", self.table))?;
        drop(client);
        Ok(self.rows)
    }

    pub fn table(&self) -> &'static str {
        self.table
    }
}

fn escape_into(buf: &mut Vec<u8>, s: &[u8]) {
    for &b in s {
        match b {
            b'\\' => buf.extend_from_slice(b"\\\\"),
            b'\t' => buf.extend_from_slice(b"\\t"),
            b'\n' => buf.extend_from_slice(b"\\n"),
            b'\r' => buf.extend_from_slice(b"\\r"),
            _ => buf.push(b),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escaping() {
        let mut b = Vec::new();
        escape_into(&mut b, b"a\tb\\c\nd");
        assert_eq!(b, b"a\\tb\\\\c\\nd");
    }
}
