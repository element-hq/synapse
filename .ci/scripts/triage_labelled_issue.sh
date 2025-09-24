#!/usr/bin/env bash
set -euo pipefail

# 1) Resolve project ID.
PROJECT_ID=$(gh project view "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --format json | jq -r '.id')

# 2) Find existing item (project card) for this issue.
ITEM_ID=$(
  gh project item-list "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --format json \
  | jq -r --arg url "$ISSUE_URL" '.items[] | select(.content.url==$url) | .id' | head -n1
)

# 3) If one doesn't exist, add this issue to the project.
if [ -z "${ITEM_ID:-}" ]; then
  ITEM_ID=$(gh project item-add "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --url "$ISSUE_URL" --format json | jq -r '.id')
fi

# 4) Get Status field id + the option id for TARGET_STATUS.
FIELDS_JSON=$(gh project field-list "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" --format json)
STATUS_FIELD=$(echo "$FIELDS_JSON" | jq -r '.fields[] | select(.name=="Status")')
STATUS_FIELD_ID=$(echo "$STATUS_FIELD" | jq -r '.id')
OPTION_ID=$(echo "$STATUS_FIELD" | jq -r --arg name "$TARGET_STATUS" '.options[] | select(.name==$name) | .id')

if [ -z "${OPTION_ID:-}" ]; then
  echo "No Status option named \"$TARGET_STATUS\" found"; exit 1
fi

# 5) Set Status (moves item to the matching column in the board view).
gh project item-edit --id "$ITEM_ID" --project-id "$PROJECT_ID" --field-id "$STATUS_FIELD_ID" --single-select-option-id "$OPTION_ID"