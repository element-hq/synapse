use std::{
    collections::BTreeMap,
    fs::File,
    io::{BufWriter, Write},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender},
        Arc, OnceLock,
    },
    time::{SystemTime, UNIX_EPOCH},
};

use anyhow::{bail, Context};
use pyo3::{
    pyclass, pymethods,
    types::{PyAnyMethods, PyModule, PyModuleMethods},
    Bound, PyAny, PyResult, Python,
};

struct Row {
    cache: u16,
    time_ms: i64,
    hash: u64,
    op: Op,
}

enum Op {
    Register { cache_name: String },
    New { key_size: u64, value_size: u64 },
    Request,
    Invalidate,
    Evict,
}

#[pyclass]
pub struct CacheTracer {
    tx: SyncSender<Row>,
    error_flag: Arc<AtomicBool>,
    cache_names: BTreeMap<String, u16>,
}

#[pymethods]
impl CacheTracer {
    #[new]
    #[pyo3(signature = ())]
    pub fn py_new() -> Self {
        let (tx, rx) = mpsc::sync_channel(2048);
        let error_flag = Arc::new(AtomicBool::new(false));
        std::thread::spawn({
            let error_flag = Arc::clone(&error_flag);
            move || {
                if let Err(err) = receive_and_log_traces(rx, error_flag) {
                    eprintln!("error in cache tracer: {err}");
                }
            }
        });
        CacheTracer {
            tx,
            error_flag,
            cache_names: BTreeMap::new(),
        }
    }

    #[pyo3(signature = (cache, key, value))]
    pub fn on_new(
        &mut self,
        py: Python<'_>,
        cache: &str,
        key: Bound<'_, PyAny>,
        value: Bound<'_, PyAny>,
    ) {
        let key_hash = key.hash().unwrap() as u64;
        let key_size = get_size_of(py, &key);
        let value_size = get_size_of(py, &value);

        let cache_id = if let Some(cache_id) = self.cache_names.get(cache) {
            *cache_id
        } else {
            let new = self.cache_names.len() as u16;
            self.cache_names.insert(cache.to_owned(), new);
            let _ = self.tx.try_send(Row {
                cache: new,
                op: Op::Register {
                    cache_name: cache.to_owned(),
                },
                hash: 0,
                time_ms: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64,
            });
            new
        };

        if let Err(_e) = self.tx.try_send(Row {
            cache: cache_id,
            op: Op::New {
                key_size,
                value_size,
            },
            hash: key_hash,
            time_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as i64,
        }) {
            self.error_flag.store(true, Ordering::Relaxed);
        }
    }

    #[pyo3(signature = (cache, key))]
    pub fn on_request(&mut self, _py: Python<'_>, cache: &str, key: Bound<'_, PyAny>) {
        let key_hash = key.hash().unwrap() as u64;

        let cache_id = if let Some(cache_id) = self.cache_names.get(cache) {
            *cache_id
        } else {
            let new = self.cache_names.len() as u16;
            self.cache_names.insert(cache.to_owned(), new);
            let _ = self.tx.try_send(Row {
                cache: new,
                op: Op::Register {
                    cache_name: cache.to_owned(),
                },
                hash: 0,
                time_ms: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64,
            });
            new
        };

        if let Err(_e) = self.tx.try_send(Row {
            cache: cache_id,
            op: Op::Request,
            hash: key_hash,
            time_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as i64,
        }) {
            self.error_flag.store(true, Ordering::Relaxed);
        }
    }

    #[pyo3(signature = (cache, key))]
    pub fn on_invalidate(&mut self, _py: Python<'_>, cache: &str, key: Bound<'_, PyAny>) {
        let key_hash = key.hash().unwrap() as u64;

        let cache_id = if let Some(cache_id) = self.cache_names.get(cache) {
            *cache_id
        } else {
            let new = self.cache_names.len() as u16;
            self.cache_names.insert(cache.to_owned(), new);
            let _ = self.tx.try_send(Row {
                cache: new,
                op: Op::Register {
                    cache_name: cache.to_owned(),
                },
                hash: 0,
                time_ms: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64,
            });
            new
        };

        if let Err(_e) = self.tx.try_send(Row {
            cache: cache_id,
            op: Op::Invalidate,
            hash: key_hash,
            time_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as i64,
        }) {
            self.error_flag.store(true, Ordering::Relaxed);
        }
    }

    #[pyo3(signature = (cache, key))]
    pub fn on_evict(&mut self, _py: Python<'_>, cache: &str, key: Bound<'_, PyAny>) {
        let key_hash = key.hash().unwrap() as u64;

        let cache_id = if let Some(cache_id) = self.cache_names.get(cache) {
            *cache_id
        } else {
            let new = self.cache_names.len() as u16;
            self.cache_names.insert(cache.to_owned(), new);
            let _ = self.tx.try_send(Row {
                cache: new,
                op: Op::Register {
                    cache_name: cache.to_owned(),
                },
                hash: 0,
                time_ms: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_millis() as i64,
            });
            new
        };

        if let Err(_e) = self.tx.try_send(Row {
            cache: cache_id,
            op: Op::Evict,
            hash: key_hash,
            time_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_millis() as i64,
        }) {
            self.error_flag.store(true, Ordering::Relaxed);
        }
    }
}

static GETSIZEOF: OnceLock<pyo3::Py<pyo3::PyAny>> = OnceLock::new();

fn get_size_of(py: Python<'_>, obj: &Bound<'_, PyAny>) -> u64 {
    let getsizeof = GETSIZEOF.get_or_init(|| {
        let sys = PyModule::import(py, "synapse.util.caches.lrucache").unwrap();
        let func = sys.getattr("_get_size_of").unwrap().unbind();
        func
    });

    let size: u64 = getsizeof.call1(py, (obj,)).unwrap().extract(py).unwrap();
    size
}

fn receive_and_log_traces(rx: Receiver<Row>, error_flag: Arc<AtomicBool>) -> anyhow::Result<()> {
    let pid = std::process::id();
    let f = File::create_new(format!("/tmp/syncachetrace-{pid}"))
        .context("failed to start cache tracer")?;
    let mut bw = BufWriter::new(f);

    let mut last_time = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;

    while let Ok(row) = rx.recv() {
        if error_flag.load(Ordering::Relaxed) {
            bw.write_all(b"DEADBEEF")?;
            bw.flush()?;
            bail!("error flagged");
        }

        let time_delta = row.time_ms.saturating_sub(last_time);
        last_time = row.time_ms;
        bw.write_all(&(time_delta as i16).to_be_bytes())?;
        bw.write_all(&row.cache.to_be_bytes())?;
        bw.write_all(&row.hash.to_be_bytes())?;

        match row.op {
            Op::Register { cache_name } => {
                bw.write_all(b"*")?;
                bw.write_all(&(cache_name.len() as u32).to_be_bytes())?;
                bw.write_all(cache_name.as_bytes())?;
            }
            Op::New {
                key_size,
                value_size,
            } => {
                let key_size = key_size.min(u32::MAX as u64) as u32;
                let value_size = value_size.min(u32::MAX as u64) as u32;
                bw.write_all(b"N")?;
                bw.write_all(&key_size.to_be_bytes())?;
                bw.write_all(&value_size.to_be_bytes())?;
            }
            Op::Request => {
                bw.write_all(b"R")?;
            }
            Op::Invalidate => {
                bw.write_all(b"I")?;
            }
            Op::Evict => {
                bw.write_all(b"E")?;
            }
        }
    }

    Ok(())
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "tmp_cachetrace")?;
    child_module.add_class::<CacheTracer>()?;

    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.tmp_cachetrace", child_module)?;

    Ok(())
}
