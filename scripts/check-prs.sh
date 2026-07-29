#!/bin/bash
set -e

CONFIG_FILE="${1:?Usage: check-prs.sh <config-file>}"
REPO=$(python3 -c "import yaml; c=yaml.safe_load(open('${CONFIG_FILE}')); print(c['repo'])")
SINCE=$(date -u -d '30 minutes ago' '+%Y-%m-%dT%H:%M:%SZ')
BASE=$(basename "${CONFIG_FILE}" .yml)
ALERTED_FILE="alerted/${BASE%-config}-alerted.txt"

touch "$ALERTED_FILE"

is_alerted() {
  grep -q "^$1$" "$ALERTED_FILE" 2>/dev/null
}

process_pr() {
  local PR=$1

  if is_alerted "$PR"; then
    echo "PR #${PR} already alerted, skipping"
    return
  fi

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

# Check newly created PRs
echo "Checking for new PRs since ${SINCE}..."
NEW_PRS=$(curl -s \
  "https://api.github.com/search/issues?q=repo:${REPO}+is:pr+created:>${SINCE}&sort=created&order=desc")
NEW_PR_NUMBERS=$(echo "$NEW_PRS" | jq -r '.items[].number')

for PR in $NEW_PR_NUMBERS; do
  process_pr "$PR"
done

# Check recently updated PRs
echo "Checking for updated PRs since ${SINCE}..."
UPDATED_PRS=$(curl -s \
  "https://api.github.com/search/issues?q=repo:${REPO}+is:pr+is:open+updated:>${SINCE}&sort=updated&order=desc")
UPDATED_PR_NUMBERS=$(echo "$UPDATED_PRS" | jq -r '.items[].number')

for PR in $UPDATED_PR_NUMBERS; do
  process_pr "$PR"
done
