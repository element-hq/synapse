//! Backend-agnostic value and row types for the Rust-native query helpers.
//!
//! [`DbValue`] is the common currency for both query *parameters* and result
//! *cells*, so simple queries can be written once and run against any backend
//! (currently Postgres; SQLite to follow). Each backend maps its driver's own
//! value types to and from [`DbValue`] at its edge — see
//! `postgres::value` for the Postgres mapping.
//!
//! The set is deliberately small (the common scalar types); richer types can be
//! added as the query helpers grow to need them.

/// A single backend-agnostic value, used both as a query parameter and as a
/// cell in a returned [`DbRow`].
#[derive(Debug, Clone, PartialEq)]
pub enum DbValue {
    /// SQL `NULL`.
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    Bytes(Vec<u8>),
}

// Ergonomic conversions so callers can write `&[1i64.into(), "x".into()]` (or
// pass typed values straight through) rather than spelling out `DbValue`.
impl From<bool> for DbValue {
    fn from(v: bool) -> Self {
        DbValue::Bool(v)
    }
}
impl From<i64> for DbValue {
    fn from(v: i64) -> Self {
        DbValue::Int(v)
    }
}
impl From<i32> for DbValue {
    fn from(v: i32) -> Self {
        DbValue::Int(v as i64)
    }
}
impl From<f64> for DbValue {
    fn from(v: f64) -> Self {
        DbValue::Float(v)
    }
}
impl From<String> for DbValue {
    fn from(v: String) -> Self {
        DbValue::Text(v)
    }
}
impl From<&str> for DbValue {
    fn from(v: &str) -> Self {
        DbValue::Text(v.to_owned())
    }
}
impl From<Vec<u8>> for DbValue {
    fn from(v: Vec<u8>) -> Self {
        DbValue::Bytes(v)
    }
}
impl From<&[u8]> for DbValue {
    fn from(v: &[u8]) -> Self {
        DbValue::Bytes(v.to_vec())
    }
}
impl<T: Into<DbValue>> From<Option<T>> for DbValue {
    fn from(v: Option<T>) -> Self {
        match v {
            Some(v) => v.into(),
            None => DbValue::Null,
        }
    }
}

/// A row of data returned by a query: one [`DbValue`] per column, read out by
/// numeric index with [`DbRowExt::try_get`].
pub type DbRow = Vec<DbValue>;

/// Reads a typed value out of a [`DbValue`], analogous to `tokio_postgres`'s
/// `FromSql`. `Option<T>` reads a nullable column (SQL `NULL` -> `None`).
pub trait FromDbValue: Sized {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error>;
}

impl FromDbValue for bool {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Bool(b) => Ok(b),
            // SQLite has no native boolean type and stores them as integers.
            DbValue::Int(i) => Ok(i != 0),
            other => anyhow::bail!("cannot read {other:?} as bool"),
        }
    }
}

impl FromDbValue for i64 {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Int(i) => Ok(i),
            DbValue::Bool(b) => Ok(b as i64),
            other => anyhow::bail!("cannot read {other:?} as i64"),
        }
    }
}

impl FromDbValue for f64 {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Float(f) => Ok(f),
            DbValue::Int(i) => Ok(i as f64),
            other => anyhow::bail!("cannot read {other:?} as f64"),
        }
    }
}

impl FromDbValue for String {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Text(s) => Ok(s),
            other => anyhow::bail!("cannot read {other:?} as String"),
        }
    }
}

impl FromDbValue for Vec<u8> {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Bytes(b) => Ok(b),
            other => anyhow::bail!("cannot read {other:?} as bytes"),
        }
    }
}

impl<T: FromDbValue> FromDbValue for Option<T> {
    fn from_value(value: DbValue) -> Result<Self, anyhow::Error> {
        match value {
            DbValue::Null => Ok(None),
            other => Ok(Some(T::from_value(other)?)),
        }
    }
}

/// Extension methods for reading typed values out of a [`DbRow`].
pub trait DbRowExt {
    /// Read the value at `index`, converting it into `T` via [`FromDbValue`].
    /// Errors if the index is out of bounds or the value can't become `T`.
    fn try_get<T: FromDbValue>(&self, index: usize) -> Result<T, anyhow::Error>;
}

impl DbRowExt for DbRow {
    fn try_get<T: FromDbValue>(&self, index: usize) -> Result<T, anyhow::Error> {
        let value = self.get(index).cloned().ok_or_else(|| {
            anyhow::anyhow!(
                "tried to read column {index} but the row only has {} column(s)",
                self.len()
            )
        })?;
        T::from_value(value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn from_impls_classify_scalars() {
        assert_eq!(DbValue::from(true), DbValue::Bool(true));
        assert_eq!(DbValue::from(7i64), DbValue::Int(7));
        assert_eq!(DbValue::from(7i32), DbValue::Int(7));
        assert_eq!(DbValue::from(1.5f64), DbValue::Float(1.5));
        assert_eq!(DbValue::from("x"), DbValue::Text("x".to_owned()));
        assert_eq!(DbValue::from(vec![1u8, 2]), DbValue::Bytes(vec![1, 2]));
        // `Option` folds `None` to NULL and `Some` to the inner conversion.
        assert_eq!(DbValue::from(None::<i64>), DbValue::Null);
        assert_eq!(DbValue::from(Some(3i64)), DbValue::Int(3));
    }

    #[test]
    fn try_get_reads_typed_values() {
        let row: DbRow = vec![
            DbValue::Int(42),
            DbValue::Text("hi".to_owned()),
            DbValue::Null,
        ];
        assert_eq!(row.try_get::<i64>(0).unwrap(), 42);
        assert_eq!(row.try_get::<String>(1).unwrap(), "hi");
        // A NULL read as `Option` is `None`...
        assert_eq!(row.try_get::<Option<String>>(2).unwrap(), None);
        // ...but read as a non-optional type it's an error, not a silent default.
        assert!(row.try_get::<String>(2).is_err());
    }

    #[test]
    fn try_get_out_of_bounds_errors() {
        let row: DbRow = vec![DbValue::Int(1)];
        let err = row.try_get::<i64>(5).unwrap_err();
        assert!(err.to_string().contains("only has 1 column"), "{err}");
    }

    #[test]
    fn bool_reads_from_sqlite_style_integer() {
        // SQLite returns booleans as integers; `FromDbValue for bool` bridges it.
        assert!(bool::from_value(DbValue::Int(1)).unwrap());
        assert!(!bool::from_value(DbValue::Int(0)).unwrap());
    }
}
