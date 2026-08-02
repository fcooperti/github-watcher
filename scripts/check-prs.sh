#!/bin/bash
set -e

CONFIG_FILE="${1:?Usage: check-prs.sh <config-file>}"
REPO=$(python3 -c "import yaml; c=yaml.safe_load(open('${CONFIG_FILE}')); print(c['repo'])")
BASE=$(basename "${CONFIG_FILE}" .yml)
ALERTED_FILE="alerted/${BASE%-config}-alerted.txt"
LAST_RUN_FILE="alerted/${BASE%-config}-last-run.txt"

GITHUB_AUTH=()
if [ -n "$GITHUB_TOKEN" ]; then
  GITHUB_AUTH=(-H "Authorization: Bearer $GITHUB_TOKEN")
  MAX_PRS=${MAX_PRS_PER_RUN:-500}
  echo "Using authenticated GitHub API"
else
  MAX_PRS=${MAX_PRS_PER_RUN:-25}
fi

touch "$ALERTED_FILE"

LOOKBACK_DAYS=$(python3 -c "import yaml; c=yaml.safe_load(open('${CONFIG_FILE}')); print(c.get('initial_lookback_days', 365))")
DEFAULT_SINCE=$(date -u -d "${LOOKBACK_DAYS} days ago" '+%Y-%m-%dT%H:%M:%SZ')

# Two separate cursors: created_at for new-PRs query, updated_at for updated-PRs query
CREATED_SINCE=""
UPDATED_SINCE=""
if [ -f "$LAST_RUN_FILE" ]; then
  CREATED_SINCE=$(grep "^created_since=" "$LAST_RUN_FILE" | cut -d= -f2)
  UPDATED_SINCE=$(grep "^updated_since=" "$LAST_RUN_FILE" | cut -d= -f2)
fi

if [ -z "$CREATED_SINCE" ]; then
  CREATED_SINCE="$DEFAULT_SINCE"
  echo "No created cursor found, scanning back ${LOOKBACK_DAYS} days to ${CREATED_SINCE}"
else
  echo "Resuming created cursor from: ${CREATED_SINCE}"
fi

if [ -z "$UPDATED_SINCE" ]; then
  UPDATED_SINCE="$DEFAULT_SINCE"
  echo "No updated cursor found, scanning back ${LOOKBACK_DAYS} days to ${UPDATED_SINCE}"
else
  echo "Resuming updated cursor from: ${UPDATED_SINCE}"
fi

LATEST_CREATED_TS=""
LATEST_UPDATED_TS=""
PROCESSED=0

# Update a named variable with ts if ts is newer than current value
update_ts() {
  local var="$1"
  local ts="$2"
  local current="${!var}"
  if [ -z "$current" ] || [[ "$ts" > "$current" ]]; then
    printf -v "$var" '%s' "$ts"
  fi
}

is_alerted() {
  grep -q "^$1$" "$ALERTED_FILE" 2>/dev/null
}

process_pr() {
  local PR=$1
  local CREATED_AT="${2%T*}"  # trim to date only: 2025-08-19

  PR_DETAIL=$(curl -s "${GITHUB_AUTH[@]}" "https://api.github.com/repos/${REPO}/pulls/${PR}")

  STATE=$(echo "$PR_DETAIL" | jq -r '.state')
  if [ "$STATE" != "open" ]; then
    return
  fi

  TITLE=$(echo "$PR_DETAIL" | jq -r '.title')
  AUTHOR=$(echo "$PR_DETAIL" | jq -r '.user.login')
  URL=$(echo "$PR_DETAIL" | jq -r '.html_url')

  CHANGED_FILES=$(curl -s "${GITHUB_AUTH[@]}" \
    "https://api.github.com/repos/${REPO}/pulls/${PR}/files" \
    | jq -r '.[].filename')

  EMAIL_MAP=$(echo "$CHANGED_FILES" | python3 scripts/map_emails.py "$CONFIG_FILE")

  EMAIL_COUNT=$(echo "$EMAIL_MAP" | jq 'length')

  if [ "$EMAIL_COUNT" -gt 0 ]; then
    echo "PR #${PR} (${CREATED_AT}): match found — alerting ${EMAIL_COUNT} recipient(s)"
    echo "$PR" >> "$ALERTED_FILE"

    while IFS= read -r EMAIL; do
      MATCHED=$(echo "$EMAIL_MAP" | jq -r --arg e "$EMAIL" '.[$e] | join("\n")')

      TEXT=$(printf "PR: #%s\nTitle: %s\nAuthor: %s\nURL: %s\n\nMatched files:\n%s" \
        "$PR" "$TITLE" "$AUTHOR" "$URL" "$MATCHED")

      BODY=$(jq -n \
        --arg from "$FROM_EMAIL" \
        --arg to "$EMAIL" \
        --arg subject "Zephyr PR #${PR} touches watched files" \
        --arg text "$TEXT" \
        '{from: $from, to: $to, subject: $subject, text: $text}')

      echo "$BODY" | python3 scripts/send_email.py

      echo "  Email sent to ${EMAIL} for PR #${PR}"
    done < <(echo "$EMAIL_MAP" | jq -r 'keys[]')
  else
    echo "PR #${PR} (${CREATED_AT}): no match"
  fi
}

# ts_field: "created_at" or "updated_at" — which field to use for cursor tracking
# ts_var:   name of the global variable to advance (LATEST_CREATED_TS or LATEST_UPDATED_TS)
run_query() {
  local label="$1"
  local url="$2"
  local ts_field="$3"
  local ts_var="$4"

  echo "Checking ${label} (limit: ${MAX_PRS} new PRs)..."
  local results
  results=$(curl -s "${GITHUB_AUTH[@]}" "$url")

  while IFS=$'\t' read -r PR TS CREATED_AT; do
    [ -z "$PR" ] && continue

    if is_alerted "$PR"; then
      update_ts "$ts_var" "$TS"
      echo "PR #${PR} (${CREATED_AT%T*}) already alerted, skipping"
      continue
    fi

    if [ "$PROCESSED" -ge "$MAX_PRS" ]; then
      echo "Reached limit of ${MAX_PRS} new PRs, stopping."
      return 1
    fi

    process_pr "$PR" "$CREATED_AT"
    PROCESSED=$((PROCESSED + 1))
    update_ts "$ts_var" "$TS"
  done < <(echo "$results" | jq -r ".items[] | [(.number | tostring), .${ts_field}, .created_at] | @tsv")

  return 0
}

run_query "new PRs since ${CREATED_SINCE}" \
  "https://api.github.com/search/issues?q=repo:${REPO}+is:pr+is:open+created:>${CREATED_SINCE}&sort=created&order=asc&per_page=100" \
  "created_at" \
  "LATEST_CREATED_TS"

if [ "$PROCESSED" -lt "$MAX_PRS" ]; then
  run_query "updated PRs since ${UPDATED_SINCE}" \
    "https://api.github.com/search/issues?q=repo:${REPO}+is:pr+is:open+updated:>${UPDATED_SINCE}&sort=updated&order=asc&per_page=100" \
    "updated_at" \
    "LATEST_UPDATED_TS" || true
fi

# Save both cursors; advance to now if a query found nothing (window is empty)
NOW=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
{
  echo "created_since=${LATEST_CREATED_TS:-$NOW}"
  echo "updated_since=${LATEST_UPDATED_TS:-$NOW}"
} > "$LAST_RUN_FILE"
echo "Progress saved — created cursor: ${LATEST_CREATED_TS:-$NOW} | updated cursor: ${LATEST_UPDATED_TS:-$NOW}"
