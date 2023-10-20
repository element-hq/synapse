// Copyright 2023 The Matrix.org Foundation C.I.C.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

use std::{
    cmp::max,
    collections::{BTreeSet, HashMap, HashSet},
};

use itertools::Itertools;
use log::info;
use pyo3::{
    intern, types::PyModule, wrap_pyfunction, FromPyObject, IntoPy, PyAny, PyObject, PyResult,
    Python,
};

use pyo3::prelude::*;
pub trait ValidDatabaseFieldType {}
pub trait ValidDatabaseReturnType {}
impl ValidDatabaseFieldType for String {}
impl ValidDatabaseFieldType for usize {}
impl<T: ValidDatabaseFieldType> ValidDatabaseFieldType for Option<T> {}
impl<T0: ValidDatabaseFieldType> ValidDatabaseReturnType for (T0,) {}
impl<T0: ValidDatabaseFieldType, T1: ValidDatabaseFieldType> ValidDatabaseReturnType for (T0, T1) {}
impl<T0: ValidDatabaseFieldType, T1: ValidDatabaseFieldType, T2: ValidDatabaseFieldType>
    ValidDatabaseReturnType for (T0, T1, T2)
{
}
impl<
        T0: ValidDatabaseFieldType,
        T1: ValidDatabaseFieldType,
        T2: ValidDatabaseFieldType,
        T3: ValidDatabaseFieldType,
    > ValidDatabaseReturnType for (T0, T1, T2, T3)
{
}

/// Helper struct that accepts a boolean from the database, but also accepts an integer for SQLite compatibility.
pub struct DbBool(bool);
impl<'py> FromPyObject<'py> for DbBool {
    fn extract(ob: &'py PyAny) -> PyResult<Self> {
        #[derive(FromPyObject)]
        enum EitherIntOrBool {
            Int(i64),
            Bool(bool),
        }

        Ok(DbBool(match EitherIntOrBool::extract(ob)? {
            EitherIntOrBool::Int(int) => int != 0,
            EitherIntOrBool::Bool(bool) => bool,
        }))
    }
}
impl ValidDatabaseFieldType for DbBool {}

/// Called when registering modules with python.
pub fn register_module(py: Python<'_>, m: &PyModule) -> PyResult<()> {
    let child_module = PyModule::new(py, "db")?;

    child_module.add_function(wrap_pyfunction!(
        _get_auth_chain_difference_using_cover_index_txn,
        m
    )?)?;

    m.add_submodule(child_module)?;

    // We need to manually add the module to sys.modules to make `from
    // synapse.synapse_rust import push` work.
    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.db", child_module)?;

    Ok(())
}

/// Wrapper for a `LoggingTransaction` from the Python side of Synapse.
pub struct LoggingTransactionWrapper<'py> {
    /// The underlying LoggingTransaction
    raw: &'py PyAny,

    database_engine: DatabaseEngine,
}

impl<'source> FromPyObject<'source> for LoggingTransactionWrapper<'source> {
    fn extract(ob: &'source PyAny) -> PyResult<Self> {
        let database_engine = match ob
            .getattr("database_engine")?
            .get_type()
            .name()
            .expect("DB engine should have a type name")
        {
            "PostgresEngine" => DatabaseEngine::Postgres,
            "Sqlite3Engine" => DatabaseEngine::Sqlite,
            other => panic!("Unknown engine {other:?}"),
        };
        Ok(Self {
            raw: ob,
            database_engine,
        })
    }
}

impl<'py> LoggingTransactionWrapper<'py> {
    pub fn execute<T: IntoPy<PyObject>>(&mut self, sql: &str, args: T) -> PyResult<()> {
        let execute_fn = self.raw.getattr(intern!(self.raw.py(), "execute"))?;
        execute_fn.call1((sql, args))?;
        Ok(())
    }

    pub fn execute_values<T: IntoPy<PyObject>, R: FromPyObject<'py> + ValidDatabaseReturnType>(
        &mut self,
        sql: &str,
        args: T,
    ) -> PyResult<Vec<R>> {
        let execute_fn = self.raw.getattr(intern!(self.raw.py(), "execute_values"))?;
        Ok(execute_fn.call1((sql, args))?.extract()?)
    }

    pub fn fetchall<T: FromPyObject<'py> + ValidDatabaseReturnType>(
        &mut self,
    ) -> anyhow::Result<Vec<T>> {
        let fetch_fn = self.raw.getattr(intern!(self.raw.py(), "fetchall"))?;
        Ok(fetch_fn.call0()?.extract()?)
    }
}

#[derive(Copy, Clone, Debug)]
pub enum DatabaseEngine {
    Sqlite,
    Postgres,
}

impl DatabaseEngine {
    //[inline]
    pub fn supports_using_any_list(&self) -> bool {
        match self {
            DatabaseEngine::Sqlite => false,
            DatabaseEngine::Postgres => true,
        }
    }
}

/// Reimplementation of the equivalent in Synapse's Python code.
pub fn make_in_list_sql_clause<T: IntoPy<PyObject>>(
    database_engine: DatabaseEngine,
    column_name: &str,
    values: impl Iterator<Item = T>,
    python: Python<'_>,
) -> (String, Vec<PyObject>) {
    let list_of_values: Vec<PyObject> = values.map(|val| val.into_py(python)).collect();
    if database_engine.supports_using_any_list() {
        let list_of_values_py: PyObject = list_of_values.into_py(python);
        (format!("{column_name} = ANY(?)"), vec![list_of_values_py])
    } else {
        let mut s = String::new();
        s.push_str(column_name);
        s.push_str(" IN (");
        if list_of_values.len() > 1 {
            for _ in 1..list_of_values.len() {
                s.push_str("?,");
            }
        }
        if list_of_values.len() > 0 {
            s.push('?');
        }
        s.push(')');
        (s, list_of_values)
    }
}

/// Calculates the auth chain difference using the chain index.
///
/// See docs/auth_chain_difference_algorithm.md for details
#[pyfunction]
fn _get_auth_chain_difference_using_cover_index_txn(
    mut txn: LoggingTransactionWrapper,
    room_id: &str,
    mut state_sets: Vec<HashSet<String>>,
) -> PyResult<HashSet<String>> {
    // First we look up the chain ID/sequence numbers for all the events, and
    // work out the chain/sequence numbers reachable from each state set.

    let initial_events: HashSet<String> = state_sets
        .iter()
        .flat_map(|set| set.iter())
        .cloned()
        .collect();

    // Map from event_id -> (chain ID, seq no)
    let mut chain_info: HashMap<String, (usize, usize)> = HashMap::new();

    // Map from chain ID -> seq no -> event Id
    let mut chain_to_event: HashMap<usize, HashMap<usize, String>> = HashMap::new();

    // All the chains that we've found that are reachable from the state
    // sets.
    let mut seen_chains: HashSet<usize> = HashSet::new();

    // Fetch the chain cover index for the initial set of events we're
    // considering.
    fn fetch_chain_info<'a>(
        txn: &mut LoggingTransactionWrapper,
        events_to_fetch: impl IntoIterator<Item = &'a String>,
        chain_info: &mut HashMap<String, (usize, usize)>,
        seen_chains: &mut HashSet<usize>,
        chain_to_event: &mut HashMap<usize, HashMap<usize, String>>,
    ) -> anyhow::Result<()> {
        let sql = r#"
            SELECT event_id, chain_id, sequence_number
            FROM event_auth_chains
            WHERE
        "#;
        for batch in &events_to_fetch.into_iter().chunks(1000) {
            let rows = Python::with_gil(|py| -> anyhow::Result<_> {
                let (clause, args) =
                    make_in_list_sql_clause(txn.database_engine, "event_id", batch, py);
                txn.execute(&format!("{sql}{clause}"), args)?;
                txn.fetchall::<(String, usize, usize)>()
            })?;

            for (event_id, chain_id, sequence_number) in rows {
                // TODO would be nice to not clone the event IDs
                chain_info.insert(event_id.clone(), (chain_id, sequence_number));
                seen_chains.insert(chain_id);
                chain_to_event
                    .entry(chain_id)
                    .or_default()
                    .insert(sequence_number, event_id);
            }
        }
        Ok(())
    }

    fetch_chain_info(
        &mut txn,
        initial_events.iter(),
        &mut chain_info,
        &mut seen_chains,
        &mut chain_to_event,
    )?;

    // Check that we actually have a chain ID for all the events.
    let events_missing_chain_info: HashSet<&String> = initial_events
        .iter()
        .filter(|elem| !chain_info.contains_key(elem as &str))
        .collect();

    // The result set to return, i.e. the auth chain difference.
    let mut result: HashSet<String> = HashSet::new();

    if !events_missing_chain_info.is_empty() {
        // For some reason we have events we haven't calculated the chain
        // index for, so we need to handle those separately. This should only
        // happen for older rooms where the server doesn't have all the auth
        // events.
        match _fixup_auth_chain_difference_sets(
            &mut txn,
            room_id,
            &mut state_sets,
            &events_missing_chain_info,
            &chain_info,
        )? {
            Some(fixup) => {
                result = fixup;
            }
            None => {
                // No chain cover index!
                let exception = Python::with_gil(|py| -> anyhow::Result<PyErr> {
                    let module = py.import("synapse.storage.databases.main.event_federation")?;
                    let exception_type = module.getattr("_NoChainCoverIndex")?;
                    let exception = exception_type.call1((room_id,))?;
                    Ok(PyErr::from_value(exception))
                })?;
                return Err(exception);
            }
        }

        // We now need to refetch any events that we have added to the state
        // sets.
        let new_events_to_fetch = state_sets
            .iter()
            .flat_map(|state_set| state_set.iter())
            .filter(|event_id| !initial_events.contains(*event_id));

        fetch_chain_info(
            &mut txn,
            new_events_to_fetch,
            &mut chain_info,
            &mut seen_chains,
            &mut chain_to_event,
        )?;
    }

    // Corresponds to `state_sets`, except as a map from chain ID to max
    // sequence number reachable from the state set.
    let mut set_to_chain: Vec<HashMap<usize, usize>> = Vec::new();
    for state_set in state_sets {
        let mut chains: HashMap<usize, usize> = HashMap::new();

        for state_id in state_set {
            let (chain_id, seq_no) = chain_info[&state_id as &str];

            let chain = chains.entry(chain_id).or_insert(0);
            *chain = (*chain).max(seq_no);
        }

        set_to_chain.push(chains);
    }

    // Now we look up all links for the chains we have, adding chains to
    // set_to_chain that are reachable from each set.
    let sql = r#"
        SELECT
            origin_chain_id, origin_sequence_number,
            target_chain_id, target_sequence_number
        FROM event_auth_chain_links
        WHERE 
    "#;

    // (We need to take a copy of `seen_chains` as we want to mutate it in
    // the loop)
    // TODO might be wise to avoid this clone
    for batch2 in &seen_chains.clone().into_iter().chunks(1000) {
        Python::with_gil(|py| -> anyhow::Result<()> {
            let (clause, args) =
                make_in_list_sql_clause(txn.database_engine, "origin_chain_id", batch2, py);
            txn.execute(&format!("{sql}{clause}"), args)?;
            Ok(())
        })?;

        for (origin_chain_id, origin_sequence_number, target_chain_id, target_sequence_number) in
            txn.fetchall::<(usize, usize, usize, usize)>()?
        {
            for chains in &mut set_to_chain {
                // chains are only reachable if the origin sequence number of
                // the link is less than the max sequence number in the
                // origin chain.
                if origin_sequence_number <= chains.get(&origin_chain_id).copied().unwrap_or(0) {
                    chains.insert(
                        target_chain_id,
                        max(
                            target_sequence_number,
                            chains.get(&target_chain_id).copied().unwrap_or(0),
                        ),
                    );
                }
            }

            seen_chains.insert(target_chain_id);
        }
    }

    // Now for each chain we figure out the maximum sequence number reachable
    // from *any* state set and the minimum sequence number reachable from
    // *all* state sets. Events in that range are in the auth chain
    // difference.

    // Mapping from chain ID to the range of sequence numbers that should be
    // pulled from the database.
    let mut chain_to_gap: HashMap<usize, (usize, usize)> = HashMap::new();

    for chain_id in seen_chains {
        let (min_seq_no, max_seq_no) = set_to_chain
            .iter()
            .map(|chains| chains.get(&chain_id).copied().unwrap_or(0))
            .minmax()
            .into_option()
            .expect("this should not be empty");

        if min_seq_no < max_seq_no {
            // We have a non empty gap, try and fill it from the events that
            // we have, otherwise add them to the list of gaps to pull out
            // from the DB.
            for seq_no in (min_seq_no + 1)..(max_seq_no + 1) {
                let event_id = chain_to_event.get(&chain_id).and_then(|x| x.get(&seq_no));
                if let Some(event_id) = event_id {
                    // TODO we might like to see whether we can avoid this clone
                    result.insert((*event_id).to_owned());
                } else {
                    chain_to_gap.insert(chain_id, (min_seq_no, max_seq_no));
                    break;
                }
            }
        }
    }

    if chain_to_gap.is_empty() {
        // If there are no gaps to fetch, we're done!
        return Ok(result);
    }

    match txn.database_engine {
        DatabaseEngine::Postgres => {
            // We can use `execute_values` to efficiently fetch the gaps when
            // using postgres.
            let sql = r#"
                SELECT event_id
                FROM event_auth_chains AS c, (VALUES ?) AS l(chain_id, min_seq, max_seq)
                WHERE
                    c.chain_id = l.chain_id
                    AND min_seq < sequence_number AND sequence_number <= max_seq
            "#;

            // let args = [
            //     (chain_id, min_no, max_no)
            //     for chain_id, (min_no, max_no) in chain_to_gap.items()
            // ];
            let args: Vec<(usize, usize, usize)> = chain_to_gap
                .iter()
                .map(|(&chain_id, &(min_no, max_no))| (chain_id, min_no, max_no))
                .collect();

            let rows: Vec<(String,)> = txn.execute_values(sql, args)?;
            result.extend(rows.into_iter().map(|(r,)| r));
        }
        DatabaseEngine::Sqlite => {
            // For SQLite we just fall back to doing a noddy for loop.
            let sql = r#"
                SELECT event_id FROM event_auth_chains
                WHERE chain_id = ? AND ? < sequence_number AND sequence_number <= ?
            "#;
            for (chain_id, (min_no, max_no)) in chain_to_gap {
                txn.execute(sql, (chain_id, min_no, max_no))?;
                result.extend(txn.fetchall::<(String,)>()?.into_iter().map(|(r,)| r));
            }
        }
    }

    Ok(result)
}

/// Helper for `_get_auth_chain_difference_using_cover_index_txn` to
/// handle the case where we haven't calculated the chain cover index for
/// all events.
///
/// This modifies `state_sets` so that they only include events that have a
/// chain cover index, and returns a set of event IDs that are part of the
/// auth difference.
///
/// Returns None if there is no usable chain cover index.
fn _fixup_auth_chain_difference_sets(
    txn: &mut LoggingTransactionWrapper,
    room_id: &str,
    state_sets: &mut Vec<HashSet<String>>,
    events_missing_chain_info: &HashSet<&String>,
    events_that_have_chain_index: &HashMap<String, (usize, usize)>,
) -> anyhow::Result<Option<HashSet<String>>> {
    // This works similarly to the handling of unpersisted events in
    // `synapse.state.v2_get_auth_chain_difference`. We uses the observation
    // that if you can split the set of events into two classes X and Y,
    // where no events in Y have events in X in their auth chain, then we can
    // calculate the auth difference by considering X and Y separately.
    //
    // We do this in three steps:
    //   1. Compute the set of events without chain cover index belonging to
    //      the auth difference.
    //   2. Replacing the un-indexed events in the state_sets with their auth
    //      events, recursively, until the state_sets contain only indexed
    //      events. We can then calculate the auth difference of those state
    //      sets using the chain cover index.
    //   3. Add the results of 1 and 2 together.

    // By construction we know that all events that we haven't persisted the
    // chain cover index for are contained in
    // `event_auth_chain_to_calculate`, so we pull out the events from those
    // rather than doing recursive queries to walk the auth chain.
    //
    // We pull out those events with their auth events, which gives us enough
    // information to construct the auth chain of an event up to auth events
    // that have the chain cover index.
    let sql = r#"
        SELECT tc.event_id, ea.auth_id, eac.chain_id IS NOT NULL
        FROM event_auth_chain_to_calculate AS tc
        LEFT JOIN event_auth AS ea USING (event_id)
        LEFT JOIN event_auth_chains AS eac ON (ea.auth_id = eac.event_id)
        WHERE tc.room_id = ?
    "#;
    txn.execute(sql, (room_id,))?;
    let mut event_to_auth_ids: HashMap<String, HashSet<String>> = HashMap::new();
    let mut events_that_have_chain_index: HashSet<String> =
        events_that_have_chain_index.keys().cloned().collect();
    for (event_id, auth_id, DbBool(auth_id_has_chain)) in
        txn.fetchall::<(String, Option<String>, DbBool)>()?
    {
        let s = event_to_auth_ids.entry(event_id).or_default();
        if let Some(auth_id) = auth_id {
            s.insert(auth_id.clone());
            // (this is intended to be a BOOL but we must accept ints too?)
            if auth_id_has_chain {
                events_that_have_chain_index.insert(auth_id);
            }
        }
    }

    let events_still_without_chain_ids: HashSet<&str> = events_missing_chain_info
        .iter()
        .filter(|event_id| !event_to_auth_ids.contains_key(event_id.as_str()))
        .map(|s| s.as_str())
        .collect();
    if !events_still_without_chain_ids.is_empty() {
        // Uh oh, we somehow haven't correctly done the chain cover index,
        // bail and fall back to the old method.
        info!(
            "Unexpectedly found that events don't have chain IDs in room {}: {:?}",
            room_id, events_still_without_chain_ids,
        );

        return Ok(None);
    }

    // Create a map from event IDs we care about to their partial auth chain.
    let mut event_id_to_partial_auth_chain: HashMap<&str, HashSet<String>> = HashMap::new();
    for (event_id, auth_ids) in &event_to_auth_ids {
        if !state_sets
            .iter()
            .any(|state_set| state_set.contains(event_id.as_str()))
        {
            continue;
        }

        // TODO can we avoid the clone?
        let mut processing: BTreeSet<String> = auth_ids.iter().cloned().collect();
        let mut to_add: HashSet<String> = HashSet::new();
        while let Some(auth_id) = processing.pop_last() {
            to_add.insert(auth_id.clone());

            let sub_auth_ids = event_to_auth_ids.get(&auth_id);
            if let Some(sub_auth_ids) = sub_auth_ids {
                processing.extend(sub_auth_ids - &to_add);
            }
        }

        event_id_to_partial_auth_chain.insert(event_id, to_add);
    }

    // Now we do two things {
    //   1. Update the state sets to only include indexed events; and
    //   2. Create a new list containing the auth chains of the un-indexed
    //      events
    let mut unindexed_state_sets: Vec<HashSet<&str>> = Vec::new();
    for state_set in state_sets {
        let mut unindexed_state_set: HashSet<&str> = HashSet::new();
        for (event_id, auth_chain) in &event_id_to_partial_auth_chain {
            if !state_set.contains(*event_id) {
                continue;
            }

            unindexed_state_set.insert(event_id);

            state_set.remove(*event_id);
            // TODO is there a more efficient way to do this
            for elem in auth_chain {
                state_set.remove(elem.as_str());
            }
            for auth_id in auth_chain {
                if events_that_have_chain_index.contains(auth_id.as_str()) {
                    // TODO undesirable clone
                    state_set.insert(auth_id.clone());
                } else {
                    unindexed_state_set.insert(auth_id);
                }
            }
        }

        unindexed_state_sets.push(unindexed_state_set);
    }

    // Calculate and return the auth difference of the un-indexed events.
    // We want to return the union - intersection here.
    let mut set_presence_count: HashMap<&str, usize> = HashMap::new();
    let intersection_count = unindexed_state_sets.len();
    for elem in unindexed_state_sets
        .into_iter()
        .flat_map(|set| set.into_iter())
    {
        *set_presence_count.entry(elem).or_insert(0) += 1;
    }

    let union_minus_intersection: HashSet<String> = set_presence_count
        .into_iter()
        .filter(|(_, count)| *count < intersection_count)
        .map(|(k, _)| k.to_owned())
        .collect();

    Ok(Some(union_minus_intersection))
}
