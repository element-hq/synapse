//! The Rust-native `query` helper for the Postgres backend.
//!
//! This is the Postgres arm of [`crate::database::DbConn::query`]: it binds
//! backend-agnostic [`DbValue`] parameters and decodes rows back into
//! [`DbRow`]s, so simple queries can be written once regardless of backend.

use tokio_postgres::types::ToSql;
use tokio_postgres::Client;

use crate::database::postgres::value::{pg_row_to_db_row, PgValue};
use crate::database::value::{DbRow, DbValue};

/// Run `sql` (with `?` placeholders bound to `params`) and return all rows.
///
/// Parameters are bound positionally, in order, to the `?` placeholders — which
/// are first rewritten to Postgres' `$1, $2, ...` (see [`convert_placeholders`]).
pub(crate) async fn query(
    client: &Client,
    sql: &str,
    params: &[DbValue],
) -> Result<Vec<DbRow>, anyhow::Error> {
    let sql = convert_placeholders(sql);

    // Bind each agnostic `DbValue` as a Postgres parameter. `PgValue` is the
    // `ToSql` type the rest of the backend already uses.
    let pg_params: Vec<PgValue> = params.iter().map(PgValue::from_db_value).collect();
    let params_dyn: Vec<&(dyn ToSql + Sync)> =
        pg_params.iter().map(|v| v as &(dyn ToSql + Sync)).collect();

    // Passing the SQL as a `&str` lets `tokio_postgres` prepare it (inferring
    // the parameter types our `ToSql` impl needs) and run it in one call.
    let rows = client.query(sql.as_str(), &params_dyn).await?;

    rows.iter().map(pg_row_to_db_row).collect()
}

/// Rewrite `?` placeholders to Postgres' positional `$1, $2, ...`.
///
/// A naive left-to-right substitution, matching Synapse's existing
/// `PostgresEngine.convert_param_style` (`?` -> `%s`): it does not skip a `?`
/// inside a string literal, so callers should parameterise rather than embed a
/// literal `?` in the SQL.
fn convert_placeholders(sql: &str) -> String {
    let mut out = String::with_capacity(sql.len() + 8);
    let mut n = 0u32;
    for ch in sql.chars() {
        if ch == '?' {
            n += 1;
            out.push('$');
            out.push_str(&n.to_string());
        } else {
            out.push(ch);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn convert_placeholders_numbers_each_question_mark() {
        assert_eq!(
            convert_placeholders("SELECT * FROM t WHERE a = ? AND b = ?"),
            "SELECT * FROM t WHERE a = $1 AND b = $2"
        );
    }

    #[test]
    fn convert_placeholders_leaves_sql_without_params_untouched() {
        assert_eq!(convert_placeholders("SELECT 1"), "SELECT 1");
    }

    #[test]
    fn convert_placeholders_counts_past_nine() {
        // The index is decimal, not a single digit.
        let sql = "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)";
        let converted = convert_placeholders(sql);
        assert!(converted.ends_with("$9, $10, $11)"), "{converted}");
    }
}
