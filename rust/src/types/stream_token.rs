/*
 * This file is licensed under the Affero General Public License (AGPL) version 3.
 *
 * Copyright (C) 2026 Element Creations Ltd.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License, or (at your option) any later version.
 *
 * See the GNU Affero General Public License for more details:
 * <https://www.gnu.org/licenses/agpl-3.0.html>.
 */

//! `synapse.types.RoomStreamToken` and the pagination arithmetic over it from
//! `synapse.storage.databases.main.stream`.
//!
//! A [`RoomStreamToken`] is a position in a room's event stream: a `stream`
//! ordering, optionally a `topological` ordering for historic tokens, and
//! for live tokens a vector clock of writer positions (`instance_map`) for
//! writers ahead of `stream`. The Python docstring explains the three string
//! forms. Tokens cross the boundary as the Python class, converted both
//! ways.
//!
//! The SQL helpers build the Postgres form only: the Rust sync handler reads
//! through the native Postgres driver.

use std::{cmp::min, collections::HashMap, fmt, sync::Arc};

use once_cell::sync::OnceCell;
use pyo3::{
    exceptions::PyValueError,
    intern,
    prelude::*,
    types::{PyDict, PyList, PyModule, PyModuleMethods},
};

/// Reads of a token that was not of the form the operation needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TokenError(pub String);

impl fmt::Display for TokenError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for TokenError {}

impl From<TokenError> for PyErr {
    fn from(err: TokenError) -> PyErr {
        PyValueError::new_err(err.0)
    }
}

/// A position in a room's event stream. See the module docs.
///
/// `instance_map` keeps the order it was built in, as the Python
/// `immutabledict` does, so the `m` string form comes out the same.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Default)]
pub struct RoomStreamToken {
    pub stream: i64,
    pub topological: Option<i64>,
    /// Writers ahead of `stream`, and where they are. Every position here is
    /// greater than `stream`.
    pub instance_map: Vec<(Arc<str>, i64)>,
}

impl RoomStreamToken {
    /// A live token at `stream`.
    pub fn live(stream: i64) -> Self {
        RoomStreamToken {
            stream,
            topological: None,
            instance_map: Vec::new(),
        }
    }

    /// A historic token.
    pub fn historic(topological: i64, stream: i64) -> Self {
        RoomStreamToken {
            stream,
            topological: Some(topological),
            instance_map: Vec::new(),
        }
    }

    /// A live token with a vector clock. Fails as the Python constructor
    /// does: a position at or below `stream` is invalid.
    pub fn with_instance_map(
        stream: i64,
        instance_map: Vec<(Arc<str>, i64)>,
    ) -> Result<Self, TokenError> {
        for (instance, position) in &instance_map {
            if *position <= stream {
                return Err(TokenError(format!(
                    "'instance_map' includes a stream position before the main 'stream' \
                     attribute. Instance: {instance}"
                )));
            }
        }
        Ok(RoomStreamToken {
            stream,
            topological: None,
            instance_map,
        })
    }

    /// Parse the `s` and `t` forms. The `m` form names writers by their
    /// instance IDs, which only the database can turn into names, so it takes
    /// a lookup: see [`RoomStreamToken::parse_with_instances`].
    pub fn parse(string: &str) -> Result<Self, TokenError> {
        Self::parse_with_instances(string, |_| None)
    }

    /// Parse any of the three forms. `instance_name` maps an instance ID to
    /// its name, as `get_name_from_instance_id` does, and `None` from it
    /// fails the parse.
    pub fn parse_with_instances(
        string: &str,
        instance_name: impl Fn(i64) -> Option<Arc<str>>,
    ) -> Result<Self, TokenError> {
        let invalid = || TokenError(format!("Invalid room stream token {string:?}"));
        let (kind, rest) = string.split_at_checked(1).ok_or_else(invalid)?;
        match kind {
            "s" => Ok(Self::live(rest.parse().map_err(|_| invalid())?)),
            "t" => {
                let (topological, stream) = rest.split_once('-').ok_or_else(invalid)?;
                Ok(Self::historic(
                    topological.parse().map_err(|_| invalid())?,
                    stream.parse().map_err(|_| invalid())?,
                ))
            }
            "m" => {
                let mut parts = rest.split('~');
                let stream = parts
                    .next()
                    .ok_or_else(invalid)?
                    .parse()
                    .map_err(|_| invalid())?;
                let mut instance_map = Vec::new();
                for part in parts {
                    // Tokens of the form `m5~` were created by a bug.
                    if part.is_empty() {
                        continue;
                    }
                    let (id, position) = part.split_once('.').ok_or_else(invalid)?;
                    let id: i64 = id.parse().map_err(|_| invalid())?;
                    let position: i64 = position.parse().map_err(|_| invalid())?;
                    let name = instance_name(id).ok_or_else(invalid)?;
                    instance_map.push((name, position));
                }
                Self::with_instance_map(stream, instance_map).map_err(|_| invalid())
            }
            _ => Err(invalid()),
        }
    }

    /// Serialize. `instance_id` maps a writer's name to its instance ID, as
    /// `get_id_for_instance` does. It is only consulted for the `m` form, and
    /// `None` from it is an error.
    pub fn to_string_with_instances(
        &self,
        instance_id: impl Fn(&str) -> Option<i64>,
    ) -> Result<String, TokenError> {
        if let Some(topological) = self.topological {
            return Ok(format!("t{topological}-{}", self.stream));
        }
        let mut entries = Vec::new();
        for (name, position) in &self.instance_map {
            // Ignore instances who are below the minimum stream position (we
            // might know they've advanced without seeing a recent write from
            // them).
            if *position <= self.stream {
                continue;
            }
            let id = instance_id(name)
                .ok_or_else(|| TokenError(format!("unknown instance name {name}")))?;
            entries.push(format!("{id}.{position}"));
        }
        if entries.is_empty() {
            Ok(format!("s{}", self.stream))
        } else {
            Ok(format!("m{}~{}", self.stream, entries.join("~")))
        }
    }

    pub fn is_historic(&self) -> bool {
        self.topological.is_some()
    }

    /// `(topological, stream)` of a historic token.
    pub fn as_historical_tuple(&self) -> Result<(i64, i64), TokenError> {
        match self.topological {
            Some(topological) => Ok((topological, self.stream)),
            None => Err(TokenError(
                "Cannot call `RoomStreamToken.as_historical_tuple` on live token".to_owned(),
            )),
        }
    }

    /// The maximum stream position referenced in this token. The
    /// corresponding minimum is `stream`.
    pub fn get_max_stream_pos(&self) -> i64 {
        self.instance_map
            .iter()
            .map(|(_, position)| *position)
            .max()
            .unwrap_or(self.stream)
    }

    /// The stream position the given writer was at at this token: `stream`
    /// for a writer not in the map.
    pub fn get_stream_pos_for_instance(&self, instance_name: &str) -> i64 {
        self.instance_map
            .iter()
            .find(|(name, _)| &**name == instance_name)
            .map(|(_, position)| *position)
            .unwrap_or(self.stream)
    }

    /// Whether every constituent part of this token is at or before the
    /// other's: `self <= other`. Not a total order: `!a.is_before_or_eq(b)`
    /// does not imply `b.is_before_or_eq(a)`.
    pub fn is_before_or_eq(&self, other: &RoomStreamToken) -> bool {
        if self.stream > other.stream {
            return false;
        }
        self.instance_map
            .iter()
            .chain(&other.instance_map)
            .all(|(name, _)| {
                self.get_stream_pos_for_instance(name) <= other.get_stream_pos_for_instance(name)
            })
    }

    /// A token such that an event after both this token and `other` is after
    /// it too. Live tokens only.
    pub fn copy_and_advance(&self, other: &RoomStreamToken) -> Result<Self, TokenError> {
        if self.is_historic() || other.is_historic() {
            return Err(TokenError("Can't advance topological tokens".to_owned()));
        }
        let max_stream = self.stream.max(other.stream);
        // Python iterates `set(self.instance_map).union(other.instance_map)`,
        // whose order is unspecified, so the order here is ours to choose:
        // this token's writers, then the other's new ones.
        let mut instance_map: Vec<(Arc<str>, i64)> = Vec::new();
        for (name, _) in self.instance_map.iter().chain(&other.instance_map) {
            if instance_map.iter().any(|(seen, _)| seen == name) {
                continue;
            }
            let position = self
                .get_stream_pos_for_instance(name)
                .max(other.get_stream_pos_for_instance(name));
            // Filter out any redundant entries.
            if position > max_stream {
                instance_map.push((Arc::clone(name), position));
            }
        }
        Ok(RoomStreamToken {
            stream: max_stream,
            topological: None,
            instance_map,
        })
    }

    /// Bound the stream positions to `max_stream`. Live tokens only.
    pub fn bound_stream_token(&self, max_stream: i64) -> Result<Self, TokenError> {
        if self.is_historic() {
            return Err(TokenError(
                "bound_stream_token only makes sense for stream tokens".to_owned(),
            ));
        }
        if self.get_max_stream_pos() <= max_stream {
            return Ok(self.clone());
        }
        let min_pos = min(self.stream, max_stream);
        let instance_map = self
            .instance_map
            .iter()
            .filter(|(_, position)| min(*position, max_stream) > min_pos)
            .map(|(name, position)| (Arc::clone(name), min(*position, max_stream)))
            .collect();
        Ok(RoomStreamToken {
            stream: min_pos,
            topological: None,
            instance_map,
        })
    }
}

/// `PersistedEventPosition.persisted_after`: whether an event persisted by
/// `instance_name` at `stream` is after `token`.
pub fn persisted_after(instance_name: &str, stream: i64, token: &RoomStreamToken) -> bool {
    token.get_stream_pos_for_instance(instance_name) < stream
}

/// The direction of a pagination.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Direction {
    Forwards,
    Backwards,
}

impl Direction {
    /// `"f"` or `"b"`, as `synapse.api.constants.Direction`.
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "f" => Some(Direction::Forwards),
            "b" => Some(Direction::Backwards),
            _ => None,
        }
    }
}

/// A bound on `(topological_ordering, stream_ordering)` for a page: the
/// topological part is absent for live tokens.
pub type Bound2 = (Option<i64>, i64);

/// `generate_pagination_bounds`: the sort order and the bounds of a page.
///
/// A live token with an instance map bounds the page by its minimum and
/// maximum positions, and the rows come back filtered by
/// [`filter_results`].
pub fn generate_pagination_bounds(
    direction: Direction,
    from_token: Option<&RoomStreamToken>,
    to_token: Option<&RoomStreamToken>,
) -> (&'static str, Option<Bound2>, Option<Bound2>) {
    let order = match direction {
        Direction::Backwards => "DESC",
        Direction::Forwards => "ASC",
    };
    let from_bound = from_token.map(|token| match (token.topological, direction) {
        (Some(topological), _) => (Some(topological), token.stream),
        (None, Direction::Backwards) => (None, token.get_max_stream_pos()),
        (None, Direction::Forwards) => (None, token.stream),
    });
    let to_bound = to_token.map(|token| match (token.topological, direction) {
        (Some(topological), _) => (Some(topological), token.stream),
        (None, Direction::Backwards) => (None, token.stream),
        (None, Direction::Forwards) => (None, token.get_max_stream_pos()),
    });
    (order, from_bound, to_bound)
}

/// `generate_pagination_where_clause`: an SQL expression bounding
/// `column_names` by the page's bounds, in Postgres's row-comparison form.
///
/// Tokens are after the row they name, so paginating forwards excludes rows
/// matching the from token and includes those matching the to token, and
/// backwards includes both.
pub fn generate_pagination_where_clause(
    direction: Direction,
    column_names: (&str, &str),
    from_bound: Option<Bound2>,
    to_bound: Option<Bound2>,
) -> String {
    let mut clauses = Vec::new();
    if let Some(from) = from_bound {
        let bound = match direction {
            Direction::Backwards => ">=",
            Direction::Forwards => "<",
        };
        clauses.push(make_generic_sql_bound(bound, column_names, from));
    }
    if let Some(to) = to_bound {
        let bound = match direction {
            Direction::Backwards => "<",
            Direction::Forwards => ">=",
        };
        clauses.push(make_generic_sql_bound(bound, column_names, to));
    }
    clauses.join(" AND ")
}

/// `_make_generic_sql_bound` for Postgres: `((v1,v2) < (c1,c2))`, or a bound
/// on the second column alone when the first value is absent. The values are
/// on the left and the columns on the right.
pub fn make_generic_sql_bound(bound: &str, column_names: (&str, &str), values: Bound2) -> String {
    debug_assert!(matches!(bound, ">" | "<" | ">=" | "<="));
    let (name1, name2) = column_names;
    match values {
        (None, val2) => format!("({val2} {bound} {name2})"),
        (Some(val1), val2) => format!("(({val1},{val2}) {bound} ({name1},{name2}))"),
    }
}

/// `generate_next_token`: the token for the next page after the last row
/// returned.
pub fn generate_next_token(
    direction: Direction,
    last_topo_ordering: Option<i64>,
    mut last_stream_ordering: i64,
) -> RoomStreamToken {
    if direction == Direction::Backwards {
        // Tokens are positions between events. This token points after the
        // last event in the chunk, and going backwards it must point to the
        // event before it, so take one off the stream part.
        last_stream_ordering -= 1;
    }
    RoomStreamToken {
        stream: last_stream_ordering,
        topological: last_topo_ordering,
        instance_map: Vec::new(),
    }
}

/// `_filter_results`: whether an event persisted by `instance_name` at the
/// given orderings falls between the tokens, `None` meaning unbounded. A
/// `None` instance name is a historic row, read as `"master"`.
///
/// This is what makes the instance map exact: the SQL fetches between the
/// minimum and maximum positions, and this keeps the rows that are after
/// the lower token and at or before the upper one for their own writer.
pub fn filter_results(
    lower_token: Option<&RoomStreamToken>,
    upper_token: Option<&RoomStreamToken>,
    instance_name: Option<&str>,
    topological_ordering: i64,
    stream_ordering: i64,
) -> bool {
    let instance_name = instance_name.unwrap_or("master");
    let event_historical_tuple = (topological_ordering, stream_ordering);

    if let Some(lower) = lower_token {
        match lower.topological {
            Some(topological) => {
                if event_historical_tuple <= (topological, lower.stream) {
                    return false;
                }
            }
            None => {
                if stream_ordering <= lower.get_stream_pos_for_instance(instance_name) {
                    return false;
                }
            }
        }
    }

    if let Some(upper) = upper_token {
        match upper.topological {
            Some(topological) => {
                if (topological, upper.stream) < event_historical_tuple {
                    return false;
                }
            }
            None => {
                if upper.get_stream_pos_for_instance(instance_name) < stream_ordering {
                    return false;
                }
            }
        }
    }

    true
}

/// `_filter_results_by_stream`: as [`filter_results`] for live tokens, by
/// stream ordering alone. Fails on a historic token.
pub fn filter_results_by_stream(
    lower_token: Option<&RoomStreamToken>,
    upper_token: Option<&RoomStreamToken>,
    instance_name: Option<&str>,
    stream_ordering: i64,
) -> Result<bool, TokenError> {
    let instance_name = instance_name.unwrap_or("master");
    if let Some(lower) = lower_token {
        if lower.is_historic() {
            return Err(TokenError("expected a live lower token".to_owned()));
        }
        if stream_ordering <= lower.get_stream_pos_for_instance(instance_name) {
            return Ok(false);
        }
    }
    if let Some(upper) = upper_token {
        if upper.is_historic() {
            return Err(TokenError("expected a live upper token".to_owned()));
        }
        if upper.get_stream_pos_for_instance(instance_name) < stream_ordering {
            return Ok(false);
        }
    }
    Ok(true)
}

// Conversion to and from the Python class.

/// A reference to the `synapse.types.RoomStreamToken` class.
static ROOM_STREAM_TOKEN_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();

fn room_stream_token_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(ROOM_STREAM_TOKEN_CLASS
        .get_or_try_init(|| -> PyResult<_> {
            Ok(py
                .import("synapse.types")?
                .getattr("RoomStreamToken")?
                .unbind())
        })?
        .bind(py))
}

/// A reference to `immutabledict.immutabledict`.
static IMMUTABLEDICT_CLASS: OnceCell<Py<PyAny>> = OnceCell::new();

fn immutabledict_class(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    Ok(IMMUTABLEDICT_CLASS
        .get_or_try_init(|| -> PyResult<_> {
            Ok(py
                .import("immutabledict")?
                .getattr("immutabledict")?
                .unbind())
        })?
        .bind(py))
}

impl<'py> FromPyObject<'_, 'py> for RoomStreamToken {
    type Error = PyErr;

    /// Read a Python `RoomStreamToken`'s attributes. No validation beyond
    /// the types: the Python constructor already checked the invariants.
    fn extract(obj: Borrowed<'_, 'py, PyAny>) -> PyResult<Self> {
        let py = obj.py();
        let stream: i64 = obj.getattr(intern!(py, "stream"))?.extract()?;
        let topological: Option<i64> = obj.getattr(intern!(py, "topological"))?.extract()?;
        let mut instance_map = Vec::new();
        for item in obj
            .getattr(intern!(py, "instance_map"))?
            .call_method0(intern!(py, "items"))?
            .try_iter()?
        {
            let (name, position): (String, i64) = item?.extract()?;
            instance_map.push((Arc::from(name), position));
        }
        Ok(RoomStreamToken {
            stream,
            topological,
            instance_map,
        })
    }
}

impl<'py> IntoPyObject<'py> for &RoomStreamToken {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
        let instance_map = PyDict::new(py);
        for (name, position) in &self.instance_map {
            instance_map.set_item(&**name, *position)?;
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "stream"), self.stream)?;
        kwargs.set_item(intern!(py, "topological"), self.topological)?;
        kwargs.set_item(
            intern!(py, "instance_map"),
            immutabledict_class(py)?.call1((instance_map,))?,
        )?;
        room_stream_token_class(py)?.call((), Some(&kwargs))
    }
}

impl<'py> IntoPyObject<'py> for RoomStreamToken {
    type Target = PyAny;
    type Output = Bound<'py, PyAny>;
    type Error = PyErr;

    fn into_pyobject(self, py: Python<'py>) -> PyResult<Self::Output> {
        (&self).into_pyobject(py)
    }
}

// Entry points for the differential tests in `tests/sync/test_rust_pure.py`,
// which compare these with the Python originals on random tokens.

/// `(is_before_or_eq, copy_and_advance, bound_stream_token(max_stream),
/// max_stream_pos)` of two live tokens, through the Rust implementation.
#[pyfunction]
fn room_stream_token_ops_for_test<'py>(
    py: Python<'py>,
    a: RoomStreamToken,
    b: RoomStreamToken,
    max_stream: i64,
) -> PyResult<(bool, Bound<'py, PyAny>, Bound<'py, PyAny>, i64)> {
    Ok((
        a.is_before_or_eq(&b),
        a.copy_and_advance(&b)?.into_pyobject(py)?,
        a.bound_stream_token(max_stream)?.into_pyobject(py)?,
        a.get_max_stream_pos(),
    ))
}

/// `to_string` through the Rust implementation, with `instance_ids` mapping
/// writer names to instance IDs, then `parse` of the result with the reverse
/// map, so the test checks the round trip too.
#[pyfunction]
fn room_stream_token_string_for_test<'py>(
    py: Python<'py>,
    token: RoomStreamToken,
    instance_ids: HashMap<String, i64>,
) -> PyResult<(String, Bound<'py, PyAny>)> {
    let string = token.to_string_with_instances(|name| instance_ids.get(name).copied())?;
    let parsed = RoomStreamToken::parse_with_instances(&string, |id| {
        instance_ids
            .iter()
            .find(|(_, &candidate)| candidate == id)
            .map(|(name, _)| Arc::from(name.as_str()))
    })?;
    Ok((string, parsed.into_pyobject(py)?))
}

/// `_filter_results` and `_filter_results_by_stream` through the Rust
/// implementation. The second is `None` for historic tokens, which it does
/// not accept.
#[pyfunction]
#[pyo3(signature = (lower, upper, instance_name, topological_ordering, stream_ordering))]
fn filter_results_for_test(
    lower: Option<RoomStreamToken>,
    upper: Option<RoomStreamToken>,
    instance_name: Option<String>,
    topological_ordering: i64,
    stream_ordering: i64,
) -> (bool, Option<bool>) {
    let by_both = filter_results(
        lower.as_ref(),
        upper.as_ref(),
        instance_name.as_deref(),
        topological_ordering,
        stream_ordering,
    );
    let by_stream = filter_results_by_stream(
        lower.as_ref(),
        upper.as_ref(),
        instance_name.as_deref(),
        stream_ordering,
    )
    .ok();
    (by_both, by_stream)
}

/// `generate_pagination_bounds` and the Postgres `generate_pagination_where_
/// clause` through the Rust implementation: `(order, from_bound, to_bound,
/// where_clause)`, with `persisted_after(instance_name, stream_ordering,
/// from)` alongside.
#[pyfunction]
#[pyo3(signature = (direction, from_token, to_token, instance_name, stream_ordering))]
#[allow(clippy::type_complexity)]
fn pagination_bounds_for_test(
    direction: &str,
    from_token: Option<RoomStreamToken>,
    to_token: Option<RoomStreamToken>,
    instance_name: String,
    stream_ordering: i64,
) -> PyResult<(
    &'static str,
    Option<Bound2>,
    Option<Bound2>,
    String,
    Option<bool>,
)> {
    let direction =
        Direction::parse(direction).ok_or_else(|| PyValueError::new_err("bad direction"))?;
    let (order, from_bound, to_bound) =
        generate_pagination_bounds(direction, from_token.as_ref(), to_token.as_ref());
    let clause = generate_pagination_where_clause(
        direction,
        ("topological_ordering", "stream_ordering"),
        from_bound,
        to_bound,
    );
    let after = from_token
        .as_ref()
        .map(|from| persisted_after(&instance_name, stream_ordering, from));
    Ok((order, from_bound, to_bound, clause, after))
}

/// `generate_next_token` through the Rust implementation.
#[pyfunction]
#[pyo3(signature = (direction, last_topo_ordering, last_stream_ordering))]
fn generate_next_token_for_test<'py>(
    py: Python<'py>,
    direction: &str,
    last_topo_ordering: Option<i64>,
    last_stream_ordering: i64,
) -> PyResult<Bound<'py, PyAny>> {
    let direction =
        Direction::parse(direction).ok_or_else(|| PyValueError::new_err("bad direction"))?;
    generate_next_token(direction, last_topo_ordering, last_stream_ordering).into_pyobject(py)
}

/// Round a list of Python tokens through the Rust type, for the conversion
/// test.
#[pyfunction]
fn round_trip_room_stream_tokens_for_test<'py>(
    py: Python<'py>,
    tokens: Vec<RoomStreamToken>,
) -> PyResult<Bound<'py, PyList>> {
    PyList::new(
        py,
        tokens
            .iter()
            .map(|t| t.into_pyobject(py))
            .collect::<PyResult<Vec<_>>>()?,
    )
}

pub fn register_test_functions(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(room_stream_token_ops_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(room_stream_token_string_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(filter_results_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(pagination_bounds_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(generate_next_token_for_test, m)?)?;
    m.add_function(wrap_pyfunction!(round_trip_room_stream_tokens_for_test, m)?)?;
    let _ = py;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn token(stream: i64, map: &[(&str, i64)]) -> RoomStreamToken {
        RoomStreamToken::with_instance_map(
            stream,
            map.iter().map(|(n, p)| (Arc::from(*n), *p)).collect(),
        )
        .unwrap()
    }

    #[test]
    fn string_forms_round_trip() {
        let ids = |name: &str| match name {
            "a" => Some(1),
            "b" => Some(2),
            _ => None,
        };
        let names = |id: i64| match id {
            1 => Some(Arc::from("a")),
            2 => Some(Arc::from("b")),
            _ => None,
        };
        for (token, expected) in [
            (RoomStreamToken::live(5), "s5"),
            (RoomStreamToken::historic(3, 5), "t3-5"),
            (token(5, &[("a", 7), ("b", 6)]), "m5~1.7~2.6"),
        ] {
            let string = token.to_string_with_instances(ids).unwrap();
            assert_eq!(string, expected);
            assert_eq!(
                RoomStreamToken::parse_with_instances(&string, names).unwrap(),
                token
            );
        }
        // A bug once wrote `m5~`.
        assert_eq!(
            RoomStreamToken::parse_with_instances("m5~", names).unwrap(),
            RoomStreamToken::live(5)
        );
        assert!(RoomStreamToken::parse("x5").is_err());
        assert!(RoomStreamToken::parse("m5~1.7").is_err());
        assert!(RoomStreamToken::with_instance_map(5, vec![(Arc::from("a"), 5)]).is_err());
    }

    #[test]
    fn ordering_and_advance() {
        let a = token(5, &[("a", 7)]);
        let b = token(6, &[("b", 8)]);
        assert!(a.is_before_or_eq(&a));
        assert!(!a.is_before_or_eq(&b)); // a is at 7 in `a`, 6 in `b`.
        assert!(!b.is_before_or_eq(&a));
        assert!(RoomStreamToken::live(5).is_before_or_eq(&a));

        let advanced = a.copy_and_advance(&b).unwrap();
        assert_eq!(advanced, token(6, &[("a", 7), ("b", 8)]));
        assert!(a.is_before_or_eq(&advanced));
        assert!(b.is_before_or_eq(&advanced));
        assert!(RoomStreamToken::historic(1, 1)
            .copy_and_advance(&a)
            .is_err());

        assert_eq!(a.get_max_stream_pos(), 7);
        assert_eq!(a.bound_stream_token(6).unwrap(), token(5, &[("a", 6)]));
        assert_eq!(a.bound_stream_token(5).unwrap(), RoomStreamToken::live(5));
        assert_eq!(a.bound_stream_token(3).unwrap(), RoomStreamToken::live(3));
        assert_eq!(a.bound_stream_token(9).unwrap(), a);
    }

    #[test]
    fn pagination_bounds_and_clause() {
        let from = token(5, &[("a", 7)]);
        let to = RoomStreamToken::historic(3, 9);
        let (order, from_bound, to_bound) =
            generate_pagination_bounds(Direction::Backwards, Some(&from), Some(&to));
        assert_eq!(
            (order, from_bound, to_bound),
            ("DESC", Some((None, 7)), Some((Some(3), 9)))
        );
        assert_eq!(
            generate_pagination_where_clause(
                Direction::Backwards,
                ("t", "s"),
                from_bound,
                to_bound
            ),
            "(7 >= s) AND ((3,9) < (t,s))"
        );
        let (order, from_bound, _) =
            generate_pagination_bounds(Direction::Forwards, Some(&from), None);
        assert_eq!((order, from_bound), ("ASC", Some((None, 5))));
        assert_eq!(
            generate_pagination_where_clause(Direction::Forwards, ("t", "s"), from_bound, None),
            "(5 < s)"
        );
        assert_eq!(
            generate_next_token(Direction::Backwards, Some(2), 10),
            RoomStreamToken::historic(2, 9)
        );
        assert_eq!(
            generate_next_token(Direction::Forwards, None, 10),
            RoomStreamToken::live(10)
        );
    }

    #[test]
    fn filtering_by_writer() {
        let lower = token(5, &[("a", 7)]);
        let upper = token(8, &[("a", 10)]);
        // Writer `a` is at 7 in `lower`, so its event at 7 is not after it.
        assert!(!filter_results(Some(&lower), Some(&upper), Some("a"), 0, 7));
        assert!(filter_results(Some(&lower), Some(&upper), Some("a"), 0, 8));
        assert!(filter_results(Some(&lower), Some(&upper), Some("a"), 0, 10));
        assert!(!filter_results(
            Some(&lower),
            Some(&upper),
            Some("a"),
            0,
            11
        ));
        // Another writer is at the minimum.
        assert!(filter_results(Some(&lower), Some(&upper), Some("b"), 0, 6));
        assert!(!filter_results(Some(&lower), Some(&upper), None, 0, 9));
        // Historic tokens compare the pair.
        let historic = RoomStreamToken::historic(3, 5);
        assert!(!filter_results(Some(&historic), None, None, 3, 5));
        assert!(filter_results(Some(&historic), None, None, 3, 6));
        assert!(filter_results(None, Some(&historic), None, 3, 5));
        assert!(!filter_results(None, Some(&historic), None, 4, 1));
        assert!(filter_results_by_stream(Some(&historic), None, None, 6).is_err());
        assert_eq!(
            filter_results_by_stream(Some(&lower), Some(&upper), Some("a"), 8),
            Ok(true)
        );
        assert!(persisted_after("a", 8, &lower));
        assert!(!persisted_after("a", 7, &lower));
        assert!(persisted_after("b", 6, &lower));
    }
}
