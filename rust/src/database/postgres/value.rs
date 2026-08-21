//! Conversions between Python values and the Postgres SQL value
//! representations.
//!
//! Supports int / float / bool / str / bytes / None. Lists (for
//! `ANY($1)`-style queries) and richer types such as json, decimal and
//! timestamps are not yet supported.
//!
//! The mapping is column-type-driven on the way *out* (a single Python `int`
//! becomes `INT2`/`INT4`/`INT8` depending on the column it is bound to) and
//! type-driven on the way *in*. The supported correspondence is:
//!
//! | [`PgValue`] variant | Python type | Postgres column type(s)             |
//! |---------------------|-------------|-------------------------------------|
//! | `Null`              | `None`      | any (encoded as SQL `NULL`)         |
//! | `Bool`              | `bool`      | `BOOL`                              |
//! | `Int`               | `int`       | `INT2`, `INT4`, `INT8`              |
//! | `Float`             | `float`     | `FLOAT4`, `FLOAT8`                  |
//! | `Text`              | `str`       | `TEXT`, `VARCHAR`, `NAME`, `BPCHAR` |
//! | `Bytea`             | `bytes`     | `BYTEA`                             |
//!
//! Both directions share these type lists via the two `accepts` methods, which
//! must stay in sync with the `match` arms below.

use std::error::Error;

use bytes::BytesMut;
use postgres_protocol::types::{
    bool_to_sql, bytea_to_sql, float4_to_sql, float8_to_sql, int2_to_sql, int4_to_sql, int8_to_sql,
    text_to_sql,
};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::pybacked::{PyBackedBytes, PyBackedStr};
use pyo3::types::{PyBool, PyBytes, PyFloat, PyInt, PyString, PyTuple};
use pyo3::{prelude::*, BoundObject};
use tokio_postgres::types::{to_sql_checked, IsNull, ToSql, Type, WrongType};

/// Owned representation of a Python value that we can hand to [`tokio_postgres`]
/// as a [`ToSql`] parameter.
#[derive(Debug)]
pub enum PgValue {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(PyBackedStr),
    Bytea(PyBackedBytes),
}

impl PgValue {
    /// Classify a Python object into a [`PgValue`], or error if its type isn't
    /// one we know how to send to Postgres.
    ///
    ///   * `bool` is classified as [`PgValue::Bool`], never [`PgValue::Int`],
    ///     even though Python's `bool` is a subclass of `int`.
    ///   * `int` must fit in an `i64`; a larger Python integer raises an
    ///     `OverflowError` here, since Postgres has no wider integer type in
    ///     this mapping.
    ///   * A type we don't recognise raises `TypeError`.
    pub fn from_py(obj: Bound<PyAny>) -> PyResult<Self> {
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

        // The `cast_into` calls below are a bit awkward because we need to take
        // ownership of `obj`, so we need to use `cast_into` and then
        // `into_inner` to get the original `obj` back if the cast fails.
        let obj = match obj.cast_into::<PyString>() {
            Ok(s) => return Ok(PgValue::Text(s.try_into()?)),
            Err(err) => err.into_inner(),
        };

        let obj = match obj.cast_into::<PyBytes>() {
            Ok(s) => return Ok(PgValue::Bytea(s.into())),
            Err(err) => err.into_inner(),
        };

        Err(PyTypeError::new_err(format!(
            "unsupported parameter type for postgres: {}",
            obj.get_type().name()?,
        )))
    }
}

// Lets PyO3 extract a `PgValue` directly from a Python argument, e.g. from a
// method that takes `Option<Vec<PgValue>>` as its parameter list.
impl<'a, 'py> FromPyObject<'a, 'py> for PgValue {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        PgValue::from_py(obj.into_bound())
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
                // The `as` cast narrows to the nearest f32, losing precision.
                // Unlike the integer arms there is no fallible conversion to
                // use, since floats have no notion of "out of range", just
                // varying precision.
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
///
/// Raises `ValueError` (including the column index and its Postgres type) if a
/// column can't be decoded — e.g. its type isn't in [`PythonPgFromSql::accepts`]
/// or the wire bytes are malformed (such as non-UTF-8 data in a `TEXT` column).
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

    PyTuple::new(py, output_row)
}

/// A decoded column value, ready to drop into a Python tuple. `None` represents
/// SQL `NULL`; otherwise it holds the corresponding Python object.
///
/// Must only be used on a blocking thread (i.e. not on a tokio runtime worker
/// thread) because it attaches to the Python interpreter to build the Python
/// object. Hence why it is private.
///
/// Use [`pg_row_to_py`] directly.
struct PythonPgFromSql(pub Option<Py<PyAny>>);

impl<'a> tokio_postgres::types::FromSql<'a> for PythonPgFromSql {
    fn from_sql(ty: &Type, raw: &'a [u8]) -> Result<Self, Box<dyn Error + Sync + Send>> {
        // Decoding builds Python objects, so we need the GIL. `try_get` (our
        // only caller) already runs under it, so this attach is cheap. This
        // also only runs on the python thread and *not* the tokio runtime's
        // worker threads, so we don't need to blocking wait for the GIL.
        //
        // Ideally we'd assert that we already hold the GIL, but PyO3 doesn't
        // expose that.
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
                // Note: PyString::from_bytes expects UTF-8, and
                // postgres_protocol::types::text_from_sql demonstrates that the
                // wire bytes are indeed UTF-8, so this should be safe.
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

#[cfg(test)]
mod tests {
    //! These tests exercise the value mapping in isolation — no live Postgres
    //! server is needed. The `to_sql` / `from_sql` halves both operate on raw
    //! byte buffers, so we can drive them directly with hand-built buffers and
    //! a chosen column [`Type`], and the `from_py` classifier just needs a GIL.

    use pyo3::IntoPyObjectExt;

    use super::*;

    /// Encode `value` for column type `ty`, returning the wire bytes and the
    /// `IsNull` flag. Panics on a `ToSql` error so callers can assert on the
    /// happy path concisely.
    fn encode(value: &PgValue, ty: &Type) -> (Vec<u8>, bool) {
        let mut buf = BytesMut::new();
        let is_null = value.to_sql(ty, &mut buf).expect("encoding should succeed");
        (buf.to_vec(), matches!(is_null, IsNull::Yes))
    }

    /// Like [`encode`], but surfaces the `ToSql` result so a test can assert on
    /// the error path (e.g. an out-of-range integer).
    fn encode_result(value: &PgValue, ty: &Type) -> Result<(), Box<dyn Error + Sync + Send>> {
        let mut buf = BytesMut::new();
        value.to_sql(ty, &mut buf).map(|_| ())
    }

    /// Build a [`PgValue::Text`] from a Rust string. The variant is backed by a
    /// Python `str`, so this needs an attached interpreter.
    fn text(py: Python<'_>, s: &str) -> PgValue {
        PgValue::Text(PyString::new(py, s).try_into().unwrap())
    }

    /// Build a [`PgValue::Bytea`] from a Rust byte slice. As with [`text`], the
    /// variant is backed by a Python `bytes`.
    fn bytea(py: Python<'_>, b: &[u8]) -> PgValue {
        PgValue::Bytea(PyBytes::new(py, b).into())
    }

    #[test]
    fn from_py_classifies_supported_types() {
        Python::initialize();
        Python::attach(|py| {
            // `None` -> NULL.
            assert!(matches!(
                PgValue::from_py(py.None().into_bound(py)).unwrap(),
                PgValue::Null
            ));

            // `bool` must win over `int` (it is an `int` subclass in Python).
            assert!(matches!(
                PgValue::from_py(true.into_bound_py_any(py).unwrap()).unwrap(),
                PgValue::Bool(true)
            ));

            assert!(matches!(
                PgValue::from_py(7i64.into_bound_py_any(py).unwrap()).unwrap(),
                PgValue::Int(7)
            ));

            assert!(matches!(
                PgValue::from_py(1.5f64.into_bound_py_any(py).unwrap()).unwrap(),
                PgValue::Float(1.5)
            ));

            match PgValue::from_py("hello".into_bound_py_any(py).unwrap()).unwrap() {
                PgValue::Text(s) => assert_eq!(&*s, "hello"),
                other => panic!("expected Text, got {other:?}"),
            }

            match PgValue::from_py(PyBytes::new(py, b"\x00\xff").into_any()).unwrap() {
                PgValue::Bytea(b) => assert_eq!(&*b, b"\x00\xff"),
                other => panic!("expected Bytea, got {other:?}"),
            }
        });
    }

    #[test]
    fn from_py_extracts_via_frompyobject() {
        // Check that extracting a `PgValue` from a Python object (the
        // `FromPyObject` path) forwards to `from_py`.
        Python::initialize();
        Python::attach(|py| {
            let obj = 7i64.into_pyobject(py).unwrap().into_any();
            assert!(matches!(obj.extract::<PgValue>().unwrap(), PgValue::Int(7)));
        });
    }

    #[test]
    fn from_py_rejects_unsupported_type() {
        Python::initialize();
        Python::attach(|py| {
            // A list is not a scalar we know how to bind.
            let list = pyo3::types::PyList::new(py, [1, 2, 3]).unwrap();
            let err = PgValue::from_py(list.into_any()).unwrap_err();
            assert!(err.is_instance_of::<PyTypeError>(py));
            // The message should name the offending type.
            assert!(err.to_string().contains("list"), "got: {err}");
        });
    }

    #[test]
    fn to_sql_encodes_each_type_for_its_column() {
        // NULL is encoded as "no bytes, IsNull::Yes" regardless of column type.
        let (bytes, is_null) = encode(&PgValue::Null, &Type::INT4);
        assert!(is_null);
        assert!(bytes.is_empty());

        // Integers are width-specific: the same `Int` encodes to 2/4/8 bytes
        // depending on the column type.
        assert_eq!(encode(&PgValue::Int(1), &Type::INT2).0, 1i16.to_be_bytes());
        assert_eq!(encode(&PgValue::Int(1), &Type::INT4).0, 1i32.to_be_bytes());
        assert_eq!(encode(&PgValue::Int(1), &Type::INT8).0, 1i64.to_be_bytes());

        assert_eq!(encode(&PgValue::Bool(true), &Type::BOOL).0, vec![1]);
        assert_eq!(encode(&PgValue::Bool(false), &Type::BOOL).0, vec![0]);
        assert_eq!(
            encode(&PgValue::Float(1.0), &Type::FLOAT8).0,
            1.0f64.to_be_bytes()
        );
        Python::initialize();
        Python::attach(|py| {
            assert_eq!(encode(&text(py, "hi"), &Type::TEXT).0, b"hi".to_vec());
            assert_eq!(encode(&bytea(py, b"\x01\x02"), &Type::BYTEA).0, vec![1, 2]);
        });
    }

    #[test]
    fn to_sql_float4_narrows_with_precision_loss() {
        // `0.1` is not representable in binary floating point, so the f64 and
        // f32 encodings genuinely differ. Encoding to a FLOAT4 column must use
        // the (lossy) f32 narrowing, not reinterpret the f64 bytes.
        let value = 0.1f64;
        assert_eq!(
            encode(&PgValue::Float(value), &Type::FLOAT4).0,
            (value as f32).to_be_bytes()
        );
        // And the bytes are not just a truncation of the f64 encoding.
        assert_ne!(
            encode(&PgValue::Float(value), &Type::FLOAT4).0,
            value.to_be_bytes()[..4].to_vec()
        );
    }

    #[test]
    fn to_sql_rejects_mismatched_column_type() {
        // A value whose type doesn't match the column is a `WrongType` error
        // rather than a silent reinterpretation. Hit a few distinct arms.
        Python::initialize();
        Python::attach(|py| {
            let cases: &[(PgValue, Type)] = &[
                (PgValue::Int(1), Type::TEXT),
                (text(py, "x"), Type::INT4),
                (bytea(py, b"x"), Type::TEXT),
                (PgValue::Bool(true), Type::INT4),
                (PgValue::Float(1.0), Type::INT8),
            ];
            for (value, ty) in cases {
                let mut buf = BytesMut::new();
                assert!(
                    value.to_sql(ty, &mut buf).is_err(),
                    "expected {value:?} -> {ty} to be rejected"
                );
            }
        });
    }

    #[test]
    fn to_sql_integer_width_boundaries() {
        // Boundary values for each width encode; one step past the boundary is
        // rejected by the `try_into` guards (INT8 spans all of i64, so there is
        // nothing out of range for it).
        assert!(encode_result(&PgValue::Int(i16::MAX as i64), &Type::INT2).is_ok());
        assert!(encode_result(&PgValue::Int(i16::MIN as i64), &Type::INT2).is_ok());
        assert!(encode_result(&PgValue::Int(i16::MAX as i64 + 1), &Type::INT2).is_err());
        assert!(encode_result(&PgValue::Int(i16::MIN as i64 - 1), &Type::INT2).is_err());

        assert!(encode_result(&PgValue::Int(i32::MAX as i64), &Type::INT4).is_ok());
        assert!(encode_result(&PgValue::Int(i32::MIN as i64), &Type::INT4).is_ok());
        assert!(encode_result(&PgValue::Int(i32::MAX as i64 + 1), &Type::INT4).is_err());
        assert!(encode_result(&PgValue::Int(i32::MIN as i64 - 1), &Type::INT4).is_err());

        assert_eq!(
            encode(&PgValue::Int(i64::MAX), &Type::INT8).0,
            i64::MAX.to_be_bytes()
        );
        assert_eq!(
            encode(&PgValue::Int(i64::MIN), &Type::INT8).0,
            i64::MIN.to_be_bytes()
        );
    }

    #[test]
    fn accepts_lists_match_supported_types() {
        // The encode and decode sides must accept exactly the supported column
        // types and reject everything else; a drift between the two `accepts`
        // lists and the `match` arms would surface here.
        for ty in [
            Type::BOOL,
            Type::INT2,
            Type::INT4,
            Type::INT8,
            Type::FLOAT4,
            Type::FLOAT8,
            Type::TEXT,
            Type::VARCHAR,
            Type::NAME,
            Type::BPCHAR,
            Type::BYTEA,
        ] {
            assert!(<PgValue as ToSql>::accepts(&ty), "ToSql should accept {ty}");
            assert!(
                <PythonPgFromSql as tokio_postgres::types::FromSql>::accepts(&ty),
                "FromSql should accept {ty}"
            );
        }

        for ty in [Type::JSON, Type::TIMESTAMPTZ, Type::UUID] {
            assert!(
                !<PgValue as ToSql>::accepts(&ty),
                "ToSql should reject {ty}"
            );
            assert!(
                !<PythonPgFromSql as tokio_postgres::types::FromSql>::accepts(&ty),
                "FromSql should reject {ty}"
            );
        }
    }

    #[test]
    fn from_sql_decodes_into_python_objects() {
        use tokio_postgres::types::FromSql;

        Python::initialize();
        Python::attach(|py| {
            // Each supported type round-trips from its wire bytes to the
            // matching Python object. This mirrors the encode test's type list.
            let int2 = PythonPgFromSql::from_sql(&Type::INT2, &7i16.to_be_bytes()).unwrap();
            assert_eq!(int2.0.unwrap().extract::<i64>(py).unwrap(), 7);

            let int4 = PythonPgFromSql::from_sql(&Type::INT4, &42i32.to_be_bytes()).unwrap();
            assert_eq!(int4.0.unwrap().extract::<i64>(py).unwrap(), 42);

            // A wide INT8 that wouldn't fit in INT4, to prove the width is honoured.
            let big = i64::MAX - 1;
            let int8 = PythonPgFromSql::from_sql(&Type::INT8, &big.to_be_bytes()).unwrap();
            assert_eq!(int8.0.unwrap().extract::<i64>(py).unwrap(), big);

            // FLOAT4 decodes via an f32, then widens to a Python float.
            let f4 = PythonPgFromSql::from_sql(&Type::FLOAT4, &1.5f32.to_be_bytes()).unwrap();
            assert_eq!(f4.0.unwrap().extract::<f64>(py).unwrap(), 1.5);

            let f8 = PythonPgFromSql::from_sql(&Type::FLOAT8, &2.5f64.to_be_bytes()).unwrap();
            assert_eq!(f8.0.unwrap().extract::<f64>(py).unwrap(), 2.5);

            let b = PythonPgFromSql::from_sql(&Type::BOOL, &[1]).unwrap();
            assert!(b.0.unwrap().extract::<bool>(py).unwrap());

            let text = PythonPgFromSql::from_sql(&Type::TEXT, b"hi").unwrap();
            assert_eq!(text.0.unwrap().extract::<String>(py).unwrap(), "hi");

            // BYTEA passes raw bytes through unchanged, including NULs and
            // non-UTF-8 data — the natural inverse of the encode test.
            let bytea = PythonPgFromSql::from_sql(&Type::BYTEA, b"\x00\xff").unwrap();
            assert_eq!(
                bytea.0.unwrap().extract::<Vec<u8>>(py).unwrap(),
                b"\x00\xff"
            );

            // A SQL NULL decodes to `None` via `from_sql_null`.
            let null = PythonPgFromSql::from_sql_null(&Type::INT4).unwrap();
            assert!(null.0.is_none());
        });
    }

    #[test]
    fn from_sql_rejects_unsupported_column_type() {
        use tokio_postgres::types::FromSql;

        // The `_ =>` arm in `from_sql_with_py` guards against `accepts` drifting
        // out of sync; decoding an unsupported type is an error, not a panic.
        Python::initialize();
        Python::attach(|_py| {
            assert!(PythonPgFromSql::from_sql(&Type::JSON, b"{}").is_err());
        });
    }

    #[test]
    fn from_sql_rejects_non_utf8_text() {
        use tokio_postgres::types::FromSql;

        // TEXT decode goes through `PyString::from_bytes`, which validates
        // UTF-8: malformed bytes surface as an error rather than a panic.
        Python::initialize();
        Python::attach(|_py| {
            assert!(PythonPgFromSql::from_sql(&Type::TEXT, b"\xff\xfe").is_err());
        });
    }
}
