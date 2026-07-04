#!/usr/bin/env python
"""
This script migrates state groups from standard SQL backend to TiKV.
"""

import argparse
import json
import logging
import sys

from synapse.config.homeserver import HomeServerConfig
from synapse.storage.engines import create_engine
from synapse.synapse_rust import tikv_engine

logger = logging.getLogger("synapse_migrate_tikv")


def setup_logging() -> None:
    """Logger init."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def main() -> None:
    """Main transfer function."""
    # pylint: disable=too-many-locals,too-many-statements
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Migrate state groups to TiKV for the hybrid architecture"
    )
    parser.add_argument(
        "-c",
        "--config-path",
        action="append",
        required=True,
        help="Path to the homeserver configuration file(s)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of state groups to migrate per batch",
    )

    args = parser.parse_args()

    # Load homeserver config
    try:
        config = HomeServerConfig.load_config("", args.config_path)
    except Exception as e:
        logger.error("Failed to load config: %s", e)
        sys.exit(1)

    tikv_endpoints = config.database.tikv_pd_endpoints
    if not tikv_endpoints:
        logger.error(
            "No 'tikv_pd_endpoints' configured in database config. Ensure your config has this set."
        )
        sys.exit(1)

    logger.info("Connecting to TiKV endpoints: %s", tikv_endpoints)
    try:
        tikv_engine.open_client(tikv_endpoints)
    except Exception as e:
        logger.error("Failed to connect to TiKV cluster: %s", e)
        sys.exit(1)

    # Get primary database config
    db_config = config.database.get_single_database()
    engine = create_engine(db_config.config)

    db_params = {
        k: v
        for k, v in db_config.config.get("args", {}).items()
        if not k.startswith("cp_")
    }

    logger.info("Connecting to SQL database using %s", engine.module.__name__)
    conn = engine.module.connect(**db_params)
    cursor = conn.cursor()

    logger.info("Starting migration of state groups...")

    # Fetch all state group IDs
    cursor.execute("SELECT id FROM state_groups ORDER BY id ASC")
    all_rows = cursor.fetchall()
    all_sg_ids = [row[0] for row in all_rows]
    total_sgs = len(all_sg_ids)

    logger.info("Found %d total state groups to migrate.", total_sgs)

    batch_size = args.batch_size
    migrated_count = 0

    for i in range(0, total_sgs, batch_size):
        batch_ids = all_sg_ids[i : i + batch_size]

        # We need to query state_groups_state and state_group_edges for these IDs
        # To do this safely for both Postgres/SQLite, we'll parameterize the IN clause
        placeholders = ",".join(
            "?" if engine.module.__name__ == "sqlite3" else "%s" for _ in batch_ids
        )

        # Fetch edges
        cursor.execute(
            f"SELECT state_group, prev_state_group FROM state_group_edges WHERE state_group IN ({placeholders})",
            batch_ids,
        )
        edges = cursor.fetchall()
        edges_map = {row[0]: row[1] for row in edges}

        # Fetch state
        cursor.execute(
            f"SELECT state_group, type, state_key, event_id FROM state_groups_state WHERE state_group IN ({placeholders})",
            batch_ids,
        )
        states = cursor.fetchall()
        states_map: dict[int, list[list[str]]] = {sg_id: [] for sg_id in batch_ids}
        for row in states:
            sg_id, type_, state_key, event_id = row
            states_map[sg_id].append([type_, state_key, event_id])

        # Construct JSON payloads and push to TiKV
        tikv_pairs: list[tuple[bytes, bytes]] = []
        for sg_id in batch_ids:
            prev_group = edges_map.get(sg_id)
            deltas = states_map.get(sg_id, [])

            payload = {"prev": prev_group, "deltas": deltas}

            key = f"sg:{sg_id}".encode("utf-8")
            val = json.dumps(payload).encode("utf-8")
            tikv_pairs.append((key, val))

        if tikv_pairs:
            try:
                tikv_engine.batch_put(tikv_pairs)
            except Exception as e:
                logger.error(
                    "Failed to write batch to TiKV (batch starts at state group %d): %s",
                    batch_ids[0],
                    e,
                )
                sys.exit(1)

        migrated_count += len(batch_ids)
        logger.info(
            "Progress: %d / %d (%.2f%%)",
            migrated_count,
            total_sgs,
            (migrated_count / total_sgs) * 100,
        )

    logger.info("Migration complete! %d state groups migrated.", migrated_count)


if __name__ == "__main__":
    main()
