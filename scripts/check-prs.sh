#!/bin/bash
set -e

CONFIG_FILE="${1:?Usage: check-prs.sh <config-file>}"
REPO=$(python3 -c "import yaml; c=yaml.safe_load(open('${CONFIG_FILE}')); print(c['repo'])")
BASE=$(basename "${CONFIG_FILE}" .yml)
ALERTED_FILE="alerted/${BASE%-config}-alerted.txt"
LAST_RUN_FILE="alerted/${BASE%-config}-last-run.txt"
MAX_PRS=${MAX_PRS_PER_RUN:-25}

touch "$ALERTED_FILE"

# Use last-run timestamp as SINCE to survive downtime; fall back to 30 min ago
if [ -f "$LAST_RUN_FILE" ]; then
  SINCE=$(cat "$LAST_RUN_FILE")
  echo "Resuming from last run: ${SINCE}"
else
  SINCE=$(date -u -d '30 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')
  echo "No last-run file found, checking last 30 minutes"
fi

LATEST_TS=""
PROCESSED=0

update_latest_ts() {
  local ts="$1"
  if [ -z "$LATEST_TS" ] || [[ "$ts" > "$LATEST_TS" ]]; then
    LATEST_TS="$ts"
  fi
}

is_alerted() {
  grep -q "^$1$" "$ALERTED_FILE" 2>/dev/null
}

process_pr() {
  local PR=$1

  PR_DETAIL=$(curl -s "https://api.github.com/repos/${REPO}/pulls/${PR}")

  STATE=$(echo "$PR_DETAIL" | jq -r '.state')
  if [ "$STATE" != "open" ]; then
    return
  fi

  TITLE=$(echo "$PR_DETAIL" | jq -r '.title')
  AUTHOR=$(echo "$PR_DETAIL" | jq -r '.user.login')
  URL=$(echo "$PR_DETAIL" | jq -r '.html_url')

  CHANGED_FILES=$(curl -s \
    "https://api.github.com/repos/${REPO}/pulls/${PR}/files" \
    | jq -r '.[].filename')

  EMAIL_MAP=$(echo "$CHANGED_FILES" | python3 scripts/map_emails.py "$CONFIG_FILE")

  EMAIL_COUNT=$(echo "$EMAIL_MAP" | jq 'length')

  if [ "$EMAIL_COUNT" -gt 0 ]; then
    echo "Match found in PR #${PR} — alerting ${EMAIL_COUNT} recipient(s)"
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

      curl -s -X POST https://api.resend.com/emails \
        -H "Authorization: Bearer $RESEND_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$BODY"

      echo "Email sent to ${EMAIL} for PR #${PR}"
    done < <(echo "$EMAIL_MAP" | jq -r 'keys[]')
  fi
}

run_query() {
  local label="$1"
  local url="$2"

  echo "Checking ${label} since ${SINCE} (limit: ${MAX_PRS} new PRs)..."
  local results
  results=$(curl -s "$url")

  while IFS=$'\t' read -r PR TS; do
    [ -z "$PR" ] && continue
    update_latest_ts "$TS"

    if is_alerted "$PR"; then
      echo "PR #${PR} already alerted, skipping"
      continue
    fi

    if [ "$PROCESSED" -ge "$MAX_PRS" ]; then
      echo "Reached limit of ${MAX_PRS} new PRs, stopping. Will resume from ${LATEST_TS}."
      return 1
    fi

    process_pr "$PR"
    PROCESSED=$((PROCESSED + 1))
  done < <(echo "$results" | jq -r '.items[] | [(.number | tostring), .updated_at] | @tsv')

  return 0
}

run_query "new PRs" \
  "https://api.github.com/search/issues?q=repo:${REPO}+is:pr+created:>${SINCE}&sort=created&order=asc&per_page=100"

if [ "$PROCESSED" -lt "$MAX_PRS" ]; then
  run_query "updated PRs" \
    "https://api.github.com/search/issues?q=repo:${REPO}+is:pr+is:open+updated:>${SINCE}&sort=updated&order=asc&per_page=100" || true
fi

# Save progress — use latest timestamp seen, or current time if nothing was found
if [ -n "$LATEST_TS" ]; then
  echo "$LATEST_TS" > "$LAST_RUN_FILE"
  echo "Progress saved: cursor advanced to ${LATEST_TS}"
else
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$LAST_RUN_FILE"
  echo "No PRs found, last-run updated to now"
fi
