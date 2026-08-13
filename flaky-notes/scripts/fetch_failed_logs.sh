#!/usr/bin/env bash
# Download logs for failed complement + trial jobs found by fetch_runs_and_jobs.sh.
# Logs for a specific attempt aren't addressable per-job via the jobs/{id}/logs
# endpoint for old attempts, but it works for the attempt the job belongs to.
# Output: data/joblogs/<job_id>.log
set -euo pipefail

REPO=element-hq/synapse
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$DIR/data"
mkdir -p "$DATA/joblogs"

# Failed complement / trial jobs
jq -c 'select(.conclusion == "failure") | select(.name | test("^(complement / |trial)"))' \
  "$DATA"/jobs/*.json > "$DATA/failed_jobs.jsonl"

total=$(wc -l < "$DATA/failed_jobs.jsonl" | tr -d ' ')
echo "Downloading logs for $total failed complement/trial jobs..." >&2

i=0
while read -r job; do
  i=$((i+1))
  id=$(echo "$job" | jq -r .id)
  out="$DATA/joblogs/$id.log"
  [ -s "$out" ] && continue
  if ! gh api --allow-escape-sequences "repos/$REPO/actions/jobs/$id/logs" > "$out" 2>/dev/null; then
    # Logs expire after a while; record the miss
    echo "MISSING" > "$out.missing"
    rm -f "$out"
  fi
  if [ $((i % 10)) -eq 0 ]; then echo "  ...$i/$total logs" >&2; fi
done < "$DATA/failed_jobs.jsonl"

echo "Done downloading logs." >&2
