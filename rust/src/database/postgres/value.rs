//! Conversions between Python values and the Postgres SQL value
//! representations.
//!
//! Kept in its own module so the cursor code stays focused on the DBAPI shape
//! rather than the type-mapping table.
//!
//! First cut: int / float / bool / str / bytes / None. Lists (for
//! `ANY($1)`-style queries) and richer types — json, decimal, timestamps —
//! are deferred to a follow-up.

use std::error::Error;

use bytes::BytesMut;
use postgres_protocol::types::{
    bool_to_sql, bytea_to_sql, float4_to_sql, float8_to_sql, int2_to_sql, int4_to_sql, int8_to_sql,
    text_to_sql,
};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyString, PyTuple};
use pyo3::{prelude::*, BoundObject};
use tokio_postgres::types::{to_sql_checked, IsNull, ToSql, Type, WrongType};

/// Owned representation of a Python value that we can hand to [`tokio_postgres`]
/// as a [`ToSql`] parameter.
#[derive(Debug, Clone)]
pub enum PgValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(Box<str>),
    Bytea(Box<[u8]>),
}

impl PgValue {
    /// Classify a Python object into a [`PgValue`], or error if its type isn't
    /// one we know how to send to Postgres.
    pub fn from_py(obj: &Bound<PyAny>) -> PyResult<Self> {
        if obj.is_none() {
            return Ok(PgValue::Null);
        }
        // `bool` must be checked before `int`, since in Python `bool` is a
        // subclass of `int` and would otherwise be caught by the `PyInt` arm.
        if let Ok(b) = obj.cast::<PyBool>() {
            return Ok(PgValue::Bool(b.is_true()));
        }
        if let Ok(i) = obj.cast::<PyInt>() {
            return Ok(PgValue::Int(i.extract::<i64>()?));
        }
        if let Ok(f) = obj.cast::<PyFloat>() {
            return Ok(PgValue::Float(f.value()));
        }
        if let Ok(s) = obj.cast::<PyString>() {
            return Ok(PgValue::Text(s.to_str()?.into()));
        }
        if let Ok(b) = obj.cast::<PyBytes>() {
            return Ok(PgValue::Bytea(b.as_bytes().into()));
        }
        Err(PyTypeError::new_err(format!(
            "unsupported parameter type for postgres: {}",
            obj.get_type().name()?,
        )))
    }
}

// Lets PyO3 extract a `PgValue` directly from a Python argument, e.g. when a
// cursor method takes `Option<Vec<PgValue>>` for its parameters.
impl<'a, 'py> FromPyObject<'a, 'py> for PgValue {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        PgValue::from_py(&obj)
    }
}

/// Serialises a [`PgValue`] into Postgres' binary wire format.
///
/// The target column type (`ty`) is supplied by [`tokio_postgres`] from the
/// prepared statement, so the same `PgValue` (e.g. an `Int`) is encoded
/// differently depending on whether the column is `INT2`/`INT4`/`INT8`. A
/// value that doesn't match the column type yields a [`WrongType`] error.
impl ToSql for PgValue {
    fn to_sql(
        &self,
        ty: &Type,
        buf: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn Error + Sync + Send>> {
        match (self, ty) {
            (PgValue::Null, _) => Ok(IsNull::Yes),
            (&PgValue::Bool(v), &Type::BOOL) => {
                bool_to_sql(v, buf);
                Ok(IsNull::No)
            }
            (&PgValue::Int(i), &Type::INT2) => {
                let v = i
                    .try_into()
                    .map_err(|_| format!("integer {i} out of range for INT2"))?;
                int2_to_sql(v, buf);
                Ok(IsNull::No)
            }
            (&PgValue::Int(i), &Type::INT4) => {
                let v = i
                    .try_into()
                    .map_err(|_| format!("integer {i} out of range for INT4"))?;
                int4_to_sql(v, buf);
                Ok(IsNull::No)
            }
            (&PgValue::Int(i), &Type::INT8) => {
                int8_to_sql(i, buf);
                Ok(IsNull::No)
            }
            (&PgValue::Float(v), &Type::FLOAT4) => {
                // The `as` cast here generates the closest f32 to the f64,
                // with loss of precision. Since Python floats are variable
                // precision anyway, this is the best we can do.
                //
                // (Crucially, there is no way of doing a "fallible" cast
                // here, since unlike integers there is no notion of "out of
                // range" for floats, just varying precision.)
                float4_to_sql(v as f32, buf);
                Ok(IsNull::No)
            }
            (&PgValue::Float(v), &Type::FLOAT8) => {
                float8_to_sql(v, buf);
                Ok(IsNull::No)
            }
            (PgValue::Text(v), &Type::TEXT | &Type::VARCHAR | &Type::NAME | &Type::BPCHAR) => {
                text_to_sql(v, buf);
                Ok(IsNull::No)
            }
            (PgValue::Bytea(v), &Type::BYTEA) => {
                bytea_to_sql(v, buf);
                Ok(IsNull::No)
            }
            // If we get here then the caller has passed a value that doesn't
            // match the type of the column.
            (&PgValue::Bool(_), _) => Err(Box::new(WrongType::new::<bool>(ty.clone()))),
            (&PgValue::Int(_), _) => Err(Box::new(WrongType::new::<i64>(ty.clone()))),
            (&PgValue::Float(_), _) => Err(Box::new(WrongType::new::<f64>(ty.clone()))),
            (&PgValue::Text(_), _) => Err(Box::new(WrongType::new::<&str>(ty.clone()))),
            (&PgValue::Bytea(_), _) => Err(Box::new(WrongType::new::<&[u8]>(ty.clone()))),
        }
    }

    fn accepts(ty: &Type) -> bool {
        matches!(
            *ty,
            Type::BOOL
                | Type::INT2
                | Type::INT4
                | Type::INT8
                | Type::FLOAT4
                | Type::FLOAT8
                | Type::TEXT
                | Type::VARCHAR
                | Type::NAME
                | Type::BPCHAR
                | Type::BYTEA
        )
    }

    to_sql_checked!();
}

/// Convert a Postgres row into a Python tuple, one element per column.
///
/// Each column is decoded via [`PythonPgFromSql`], so `NULL` becomes `None` and
/// every other supported type becomes its natural Python equivalent.
pub fn pg_row_to_py<'py>(
    py: Python<'py>,
    row: &tokio_postgres::Row,
) -> PyResult<Bound<'py, PyTuple>> {
    let mut output_row = Vec::with_capacity(row.len());
    for idx in 0..row.len() {
        let obj: PythonPgFromSql = row.try_get(idx).map_err(|e| {
            PyValueError::new_err(format!(
                "failed to decode column {idx} (type {}): {e}",
                row.columns()[idx].type_()
            ))
        })?;
        output_row.push(obj.0);
    }

    Ok(PyTuple::new(py, output_row)?)
}

/// A decoded column value, ready to drop into a Python tuple. `None`
/// represents SQL `NULL`; otherwise it holds the corresponding Python object.
pub struct PythonPgFromSql(pub Option<Py<PyAny>>);

impl<'a> tokio_postgres::types::FromSql<'a> for PythonPgFromSql {
    fn from_sql(ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        // Decoding builds Python objects, so we need the GIL. `try_get` (our
        // only caller) already runs under it, so this attach is cheap.
        Python::attach(|py| Self::from_sql_with_py(py, ty, raw))
    }

    fn from_sql_null(_ty: &Type) -> Result<Self, Box<dyn Error + Sync + Send>> {
        Ok(PythonPgFromSql(None))
    }

    fn accepts(ty: &Type) -> bool {
        matches!(
            *ty,
            Type::BOOL
                | Type::INT2
                | Type::INT4
                | Type::INT8
                | Type::FLOAT4
                | Type::FLOAT8
                | Type::TEXT
                | Type::VARCHAR
                | Type::NAME
                | Type::BPCHAR
                | Type::BYTEA
        )
    }
}

impl PythonPgFromSql {
    /// Decode a non-NULL column value into the matching Python object, given
    /// an already-held GIL token.
    fn from_sql_with_py(
        py: Python<'_>,
        ty: &Type,
        raw: &[u8],
    ) -> Result<Self, Box<dyn Error + Sync + Send>> {
        let obj = match *ty {
            Type::BOOL => {
                let b = postgres_protocol::types::bool_from_sql(raw)?;
                PyBool::new(py, b).into_any().unbind()
            }
            Type::INT2 => {
                let i = postgres_protocol::types::int2_from_sql(raw)?;
                PyInt::new(py, i).into_any().unbind()
            }
            Type::INT4 => {
                let i = postgres_protocol::types::int4_from_sql(raw)?;
                PyInt::new(py, i).into_any().unbind()
            }
            Type::INT8 => {
                let i = postgres_protocol::types::int8_from_sql(raw)?;
                PyInt::new(py, i).into_any().unbind()
            }
            Type::FLOAT4 => {
                let f = postgres_protocol::types::float4_from_sql(raw)?;
                PyFloat::new(py, f.into()).into_any().unbind()
            }
            Type::FLOAT8 => {
                let f = postgres_protocol::types::float8_from_sql(raw)?;
                PyFloat::new(py, f).into_any().unbind()
            }
            Type::TEXT | Type::VARCHAR | Type::NAME | Type::BPCHAR => {
                PyString::from_bytes(py, raw)?.into_any().unbind()
            }
            Type::BYTEA => PyBytes::new(py, raw).into_any().unbind(),
            _ => {
                // This should never happen, unless the `accepts` method is out
                // of sync.
                return Err(format!("unsupported column type for postgres: {ty}").into());
            }
        };

        Ok(PythonPgFromSql(Some(obj)))
    }
}
