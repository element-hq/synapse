from synapse.config.homeserver import HomeServerConfig
from synapse.storage.database import LoggingTransaction
from synapse.storage.engines import PostgresEngine, BaseDatabaseEngine


def run_create(
    cur: LoggingTransaction,
    database_engine: BaseDatabaseEngine,
) -> None:
    """
    Drop unique constraint event_id and add unique constraint (event_id, redacts)
    """
    print("running upgrade")
    if isinstance(database_engine, PostgresEngine):
        add_constraint_sql = """
        ALTER TABLE ONLY redactions ADD CONSTRAINT redactions_event_id_redacts_key UNIQUE (event_id, redacts);
        """
        cur.execute(add_constraint_sql)

        drop_constraint_sql = """
        ALTER TABLE ONLY redactions DROP CONSTRAINT redactions_event_id_key;
        """
        cur.execute(drop_constraint_sql)

    else:
        # in SQLite we need to rewrite the table to change the constraint.
        # First drop any temporary table that might be here from a previous failed migration.
        cur.execute("DROP TABLE IF EXISTS temp_redactions")

        create_sql = """
        CREATE TABLE temp_redactions (
            event_id TEXT NOT NULL,
            redacts TEXT NOT NULL,
            have_censored BOOL NOT NULL DEFAULT false,
            received_ts BIGINT,
            UNIQUE(event_id, redacts)
        );
        """
        cur.execute(create_sql)

        copy_sql = """
        INSERT INTO temp_redactions (
            event_id,
            redacts,
            have_censored,
            received_ts
        ) SELECT r.event_id, r.redacts, r.have_censored, r.received_ts FROM redactions AS r;
        """
        cur.execute(copy_sql)

        drop_sql = """
        DROP TABLE redactions
        """
        cur.execute(drop_sql)

        rename_sql = """
        ALTER TABLE temp_redactions RENAME to redactions
        """
        cur.execute(rename_sql)

        sqlite3_idx_update_sql = """
             INSERT INTO background_updates (ordering, update_name, progress_json) \
             VALUES (?, ?, ?);
         """
        cur.execute(sqlite3_idx_update_sql, (9206, "redactions_add_redacts_idx", '{}'))
        cur.execute(sqlite3_idx_update_sql, (9206, "redactions_add_have_censored_ts", '{}'))

    # in either case the event_id index needs to be re-created
    idx_sql = """
          INSERT INTO background_updates (ordering, update_name, progress_json) VALUES (?, ?, ?);
    """
    cur.execute(idx_sql, (9206, "redactions_add_event_id_idx", '{}'))


