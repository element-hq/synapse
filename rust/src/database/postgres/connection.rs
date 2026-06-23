use std::sync::{Arc, Mutex, MutexGuard, TryLockError};

use log::warn;
use pyo3::{
    exceptions::PyRuntimeError,
    prelude::*,
    types::{PyDict, PyInt, PyTuple},
};
use tokio_postgres::Client;

use crate::database::postgres::{
    cursor_state::CursorQueryState,
    helpers::{BlockingPostgres, BlockingPostgresResult},
    value::{pg_row_to_py, PgValue},
};

#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Connection {
    pub client: Arc<Mutex<Option<Client>>>,
}

impl Connection {
    pub fn new(client: Client) -> Self {
        Self {
            client: Arc::new(Mutex::new(Some(client))),
        }
    }

    fn py_lock(&self) -> PyResult<MutexGuard<'_, Option<Client>>> {
        self.client
            .lock()
            .map_err(|_| PyRuntimeError::new_err("connection mutex poisoned"))
    }

    fn take_client(&self) -> PyResult<Client> {
        let client = self.py_lock()?.take();
        let Some(client) = client else {
            return Err(PyRuntimeError::new_err("connection already closed"));
        };
        Ok(client)
    }

    fn put_client(&self, client: Client) -> PyResult<()> {
        let mut existing_client = self.py_lock()?;
        if existing_client.is_some() {
            return Err(PyRuntimeError::new_err("connection already has a client"));
        }
        existing_client.replace(client);
        Ok(())
    }

    fn cursor<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, Cursor>, CursorGuard)> {
        let client = self.take_client()?;

        client.execute("BEGIN", &[]).block_on_result(py)?;

        let cursor = Cursor::new(self.clone(), client, true);
        let bound_cursor = Bound::new(py, cursor.clone())?;
        Ok((bound_cursor, CursorGuard { cursor }))
    }
}

#[pyclass(frozen, skip_from_py_object)]
#[derive(Clone)]
pub struct Cursor {
    inner: Arc<Mutex<CursorInner>>,
}

struct CursorInner {
    client: Option<Client>,
    connection: Connection,
    query_state: CursorQueryState,
    in_transaction: bool,
}

struct CursorGuard {
    cursor: Cursor,
}

impl Cursor {
    fn new(connection: Connection, client: Client, in_transaction: bool) -> Self {
        Self {
            inner: Arc::new(Mutex::new(CursorInner {
                client: Some(client),
                connection,
                query_state: CursorQueryState::new(),
                in_transaction,
            })),
        }
    }

    fn py_lock(&self) -> PyResult<MutexGuard<'_, CursorInner>> {
        match self.inner.try_lock() {
            Ok(guard) => Ok(guard),
            Err(TryLockError::Poisoned(_)) => {
                Err(PyRuntimeError::new_err("cursor client mutex poisoned"))
            }
            Err(TryLockError::WouldBlock) => Err(PyRuntimeError::new_err(
                "cursor is being used in another thread and cannot be used concurrently",
            )),
        }
    }

    fn take_client(&self) -> PyResult<Client> {
        let mut inner = self.py_lock()?;
        let client = inner.client.take();
        let Some(client) = client else {
            return Err(PyRuntimeError::new_err("cursor already closed"));
        };
        Ok(client)
    }

    fn finish<'py>(&self, py: Python<'py>, commit: bool) -> PyResult<()> {
        let client = self.take_client()?;

        // Mark the cursor as finished so that the `Drop` impl doesn't try to
        // rollback again. Note that this means if the `execute` call below
        // fails, we will drop the `Client` and thus close the connection.
        let mut inner = self.py_lock()?;
        inner.in_transaction = false;

        let stmt = if commit { "COMMIT" } else { "ROLLBACK" };
        client.execute(stmt, &[]).block_on_result(py)?;

        inner.connection.put_client(client)?;

        Ok(())
    }
}

#[pymethods]
impl Cursor {
    #[pyo3(signature = (query, params = None))]
    fn execute<'py>(
        &self,
        py: Python<'py>,
        query: &str,
        params: Option<Vec<PgValue>>,
    ) -> PyResult<()> {
        let mut inner = self.py_lock()?;
        inner.execute(py, query, params)
    }

    fn fetch_one<'py>(&self, py: Python<'py>) -> PyResult<Option<Vec<Option<Py<PyAny>>>>> {
        let mut inner = self.py_lock()?;
        let Some(row) = inner.query_state.fetch_one(py)? else {
            return Ok(None);
        };

        let py_row = pg_row_to_py(&row)?;
        Ok(Some(py_row))
    }

    fn fetch_all<'py>(&self, py: Python<'py>) -> PyResult<Vec<Vec<Option<Py<PyAny>>>>> {
        let mut inner = self.py_lock()?;
        let rows = inner.query_state.fetch_all(py)?;

        rows.into_iter().map(|row| pg_row_to_py(&row)).collect()
    }

    fn rowcount<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyInt>> {
        let mut inner = self.py_lock()?;
        let Some(rowcount) = inner.query_state.rowcount(py)? else {
            // If we don't have a rowcount yet, PEP-249 says we should return
            // -1.
            return Ok((-1i64).into_pyobject(py)?);
        };

        Ok(rowcount.into_pyobject(py)?)
    }
}

impl CursorInner {
    fn execute<'py>(
        &mut self,
        py: Python<'py>,
        query: &str,
        params: Option<Vec<PgValue>>,
    ) -> PyResult<()> {
        let Some(client) = &mut self.client else {
            return Err(PyRuntimeError::new_err("cursor already closed"));
        };

        self.query_state.new_query();

        let statement = &client.prepare(query).block_on_result(py)?;

        let row_stream = client
            .query_raw(statement, params.unwrap_or_default())
            .block_on_result(py)?;

        self.query_state
            .on_query_start(row_stream, statement.columns());

        Ok(())
    }
}

impl Drop for CursorGuard {
    fn drop(&mut self) {
        if let Ok(client) = self.cursor.take_client() {
            // First check if we need to rollback an uncommitted transaction. If
            // we do, try to do that before returning the client to the
            // connection so that it's in a clean state.
            //
            // Note that if we are *not* in a transaction the `ROLLBACK` is a
            // no-op, so it's safe to do this unconditionally. We only check the
            // `in_transaction` flag to avoid unnecessary queries.
            let inner = self.cursor.py_lock().unwrap();
            if inner.in_transaction {
                // We should have the GIL at this point, so the the
                // `Python::attach` should be fast. We need to attach so that we
                // can *detach*, so that we don't hold the GIL here.
                Python::attach(|py| {
                    if let Err(err) = client.execute("ROLLBACK", &[]).block_on(py) {
                        warn!("failed to rollback uncommitted transaction: {err}");
                    }
                });
            }

            // Try to return the client to the connection, but if that
            // fails, just drop it. This will close the postgres connection.
            if let Err(err) = inner.connection.put_client(client) {
                warn!("failed to return client to connection: {err}");
            }
        }
    }
}

#[pymethods]
impl Connection {
    #[pyo3(signature = (func, *args, **kwargs))]
    fn run_interaction<'py>(
        &self,
        py: Python<'py>,
        func: Bound<'py, PyAny>,
        args: Bound<'py, PyTuple>,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (cursor, _guard) = self.cursor(py)?;

        // Build a new argument list with the cursor prepended, then call the
        // provided function with that new argument list.
        let args = args.to_list();
        args.insert(0, &cursor)?;

        let result = func.call(args.to_tuple(), kwargs);

        cursor.get().finish(py, result.is_ok())?;

        result
    }
}
