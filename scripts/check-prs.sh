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

  EMAIL_MAP=$(python3 - <<EOF
import json, os, fnmatch, yaml
from collections import defaultdict
from urllib.request import urlopen

def matches_pattern(filepath, pattern):
    if pattern.endswith('/'):
        pattern_parts = pattern.rstrip('/').split('/')
        path_parts = filepath.split('/')
        if len(path_parts) <= len(pattern_parts):
            return False
        dir_prefix = '/'.join(path_parts[:len(pattern_parts)])
        return fnmatch.fnmatch(dir_prefix, pattern.rstrip('/'))
    return fnmatch.fnmatch(filepath, pattern)

with open('${CONFIG_FILE}') as f:
    config = yaml.safe_load(f)

changed = """${CHANGED_FILES}""".strip().splitlines()
email_files = defaultdict(set)
url_cache = {}

receiver_emails = yaml.safe_load(os.environ.get('RECEIVER_EMAILS', '') or '') or {}

def resolve_emails(ref):
    if ref.startswith('$'):
        key = ref[1:]
        if key in receiver_emails:
            return receiver_emails[key]
    return os.path.expandvars(ref)

def fetch_yaml(url):
    if url not in url_cache:
        with urlopen(url) as r:
            url_cache[url] = yaml.safe_load(r.read().decode())
    return url_cache[url]

for alert in config.get('alerts', []):
    alert_type = alert.get('type', 'paths')
    emails_str = resolve_emails(alert['emails'])
    emails = [e.strip() for e in emails_str.split(';') if e.strip()]

    patterns = []

    if alert_type == 'paths':
        patterns = alert.get('patterns', [])

    elif alert_type == 'yaml_section':
        external = fetch_yaml(alert['url'])
        section = external.get(alert['section'], {})
        files_key = alert.get('files_key', 'files')
        patterns = section.get(files_key, [])

    for filepath in changed:
        for pattern in patterns:
            if matches_pattern(filepath, pattern):
                for email in emails:
                    email_files[email].add(filepath)
                break

print(json.dumps({email: sorted(list(files)) for email, files in email_files.items()}))
EOF
)

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
