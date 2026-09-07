#!/usr/bin/env bash
# Fetch recent tests.yml runs and their per-job conclusions (all attempts).
# Output:
#   data/runs.jsonl          — one line per workflow run
#   data/jobs/<run_id>.json  — jobs API response (all attempts) per run
set -euo pipefail

REPO=element-hq/synapse
DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$DIR/data"
mkdir -p "$DATA/jobs"

N_RUNS="${N_RUNS:-400}"

echo "Listing last $N_RUNS tests.yml runs..." >&2
gh run list -R "$REPO" --workflow tests.yml -L "$N_RUNS" \
  --json databaseId,conclusion,status,createdAt,event,headBranch,headSha,attempt,displayTitle \
  --jq '.[]' > "$DATA/runs.jsonl"

total=$(wc -l < "$DATA/runs.jsonl" | tr -d ' ')
echo "Got $total runs. Fetching jobs (all attempts) for each..." >&2

i=0
while read -r run; do
  i=$((i+1))
  id=$(echo "$run" | jq -r .databaseId)
  status=$(echo "$run" | jq -r .status)
  out="$DATA/jobs/$id.json"
  # skip in-progress runs and already-fetched ones
  [ "$status" != "completed" ] && continue
  [ -s "$out" ] && continue
  if ! gh api -X GET "repos/$REPO/actions/runs/$id/jobs" -f filter=all -f per_page=100 --paginate \
      --jq '.jobs[] | {run_id: .run_id, run_attempt: .run_attempt, id, name, conclusion, started_at, completed_at}' \
      > "$out" 2>"$DATA/jobs/$id.err"; then
    echo "WARN: failed to fetch jobs for run $id" >&2
    rm -f "$out"
  else
    rm -f "$DATA/jobs/$id.err"
  fi
  if [ $((i % 25)) -eq 0 ]; then echo "  ...$i/$total runs processed" >&2; fi
done < "$DATA/runs.jsonl"

echo "Done fetching jobs." >&2
