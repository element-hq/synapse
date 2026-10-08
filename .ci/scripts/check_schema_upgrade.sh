#!/usr/bin/env bash
#
# Checks that you get the same resultant database schema whether you
# start at the latest Synapse version and set up a fresh database,
# or whether you set up a fresh database on an old version and then
# run the upgrade steps.
#
# Usage:
#   PGUSER=postgres PGPASSWORD=postgres .ci/scripts/check_schema_upgrade.sh

set -eu

# The full schema version to upgrade from.
BASE_SCHEMA="54"
# A git ref (e.g. tag) to check out the full schema from.
# Needed because old full schemas have since been removed.
BASE_REF="v1.62.0"

# Check Postgres env is set
: "${PGUSER:?must be set}"
: "${PGPASSWORD:?must be set}"

# Change to repo root
cd "$(dirname "$0")/../.."

# Names of the database splits we have
DATABASE_SPLITS=(common main state)

if [ ! -z "$(git status --porcelain)" ]; then
  echo "The repository has uncommitted changes. Refusing to run."
  exit 1
fi

OUTPUT_DIR="$(mktemp -d)"

echo "Checking out full schema $BASE_SCHEMA from $BASE_REF..."
for db in "${DATABASE_SPLITS[@]}"; do
  git checkout "$BASE_REF" -- "synapse/storage/schema/$db/full_schemas/$BASE_SCHEMA"
done

# Synapse will naturally set up a fresh database using the highest version full schema it can find.
# To avoid that, remove all other full schema versions.
echo "Removing other full schemas..."
for db in "${DATABASE_SPLITS[@]}"; do
  for dir in "synapse/storage/schema/$db/full_schemas"/*; do
    [ -d "$dir" ] || continue
    if [ "$(basename "$dir")" != "$BASE_SCHEMA" ]; then
      echo "  $dir"
      rm -rf "$dir"
    fi
  done
done

echo "Building the database as a new install..."
echo "$PGPASSWORD" | scripts-dev/make_full_schema.sh \
  -c -p "$PGUSER" -o "$OUTPUT_DIR/create"

echo "Building the database as an upgrade from schema $BASE_SCHEMA..."
echo "$PGPASSWORD" | scripts-dev/make_full_schema.sh \
  --test-upgrade-from="$BASE_SCHEMA" \
  -c -p "$PGUSER" -o "$OUTPUT_DIR/upgrade"

# Give a generous 10 lines of context, hopefully enough to show the table name if it's
# e.g. a column embedded within a CREATE TABLE statement.
if diff --recursive --unified=10 "$OUTPUT_DIR/create" "$OUTPUT_DIR/upgrade"; then
  echo "OK: a database upgraded from schema $BASE_SCHEMA matches a new install." >&2
else
  echo
  echo "ERROR: upgrading a database from schema $BASE_SCHEMA does not produce the same" >&2
  echo "schema as installing one at that version and applying the deltas." >&2
  echo >&2
  echo " Left (-): Fresh install     |     Right (+): Simulated upgraded install"
  exit 1
fi
