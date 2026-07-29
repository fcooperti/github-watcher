#!/usr/bin/env python3
"""Alert on newly created GitHub PRs whose files match a watcher config.

This intentionally stays as one script: config and state are plain YAML/text files,
and GitHub Actions is only the scheduler. The run flow is:
  find new PRs -> match files -> send email -> save state on full success

Required environment variables:
  EMAIL_API_KEY     Resend API key or Gmail SMTP app password
  FROM_EMAIL        Sender address; gmail.com selects Gmail, all else selects Resend
  RECEIVER_EMAILS   YAML mapping used by the config's $GROUP references

Optional environment variable:
  GITHUB_TOKEN      GitHub token for a higher API rate limit
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import yaml
from redmail import EmailSender


GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
SEARCH_PAGE_SIZE = 100
FILE_FETCH_WORKERS = 8
GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}
RETRYABLE_HTTP_CODES = {403, 429, 500, 502, 503, 504}


class WatcherError(RuntimeError):
    """A configuration, GitHub, or email delivery error."""


# Configuration and durable state

def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise WatcherError(f"{name} is required")
    return value


def load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file) or {}
    except OSError as exc:
        raise WatcherError(f"could not read config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise WatcherError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(config, dict) or not config.get("repo"):
        raise WatcherError(f"config {path} must contain repo: owner/repository")
    return config


def state_paths(config_path: Path) -> tuple[Path, Path]:
    name = config_path.stem.removesuffix("-config")
    state_dir = Path("alerted")
    return state_dir / f"{name}-alerted.txt", state_dir / f"{name}-last-run.txt"


def load_state(alerted_file: Path, last_run_file: Path, initial_lookback_days: int) -> tuple[set[str], str]:
    alerted: set[str] = set()
    if alerted_file.exists():
        alerted = {
            line.strip()
            for line in alerted_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    created_since = ""
    if last_run_file.exists():
        for line in last_run_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("created_since="):
                created_since = line.partition("=")[2].strip()
                break

    if not created_since:
        created_since = (
            datetime.now(UTC) - timedelta(days=initial_lookback_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"No cursor found; scanning PRs created since {created_since}")
    else:
        print(f"Resuming from PR creation cursor: {created_since}")

    return alerted, created_since


def atomic_write(path: Path, content: str) -> None:
    # Replacing a completed temporary file avoids leaving a half-written state file.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def save_state(alerted_file: Path, last_run_file: Path, alerted: set[str], created_since: str) -> None:
    sorted_prs = sorted(alerted, key=int)
    atomic_write(alerted_file, "\n".join(sorted_prs) + ("\n" if sorted_prs else ""))
    atomic_write(last_run_file, f"created_since={created_since}\n")


# GitHub API

def github_json(url: str, github_token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-watcher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    # Retry temporary service/network failures. A 403 can be GitHub's secondary
    # rate limit, so it is retried too; a 401 is always treated as permanent.
    for attempt in range(3):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except json.JSONDecodeError as exc:
            raise WatcherError(f"GitHub API returned invalid JSON: {url}") from exc
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_CODES or attempt == 2:
                raise WatcherError(f"GitHub API request failed ({exc.code}): {url}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                reason = getattr(exc, "reason", str(exc))
                raise WatcherError(f"could not reach GitHub API: {reason}") from exc
        time.sleep(2**attempt)

    raise AssertionError("unreachable")


def newly_created_prs(repo: str, created_since: str, github_token: str):
    query = f"repo:{repo} is:pr is:open created:>{created_since}"
    page = 1

    while True:
        url = f"{GITHUB_API}/search/issues?" + urlencode(
            {
                "q": query,
                "sort": "created",
                "order": "asc",
                "per_page": SEARCH_PAGE_SIZE,
                "page": page,
            }
        )
        result = github_json(url, github_token)
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise WatcherError("unexpected GitHub search response while listing pull requests")
        items = result["items"]
        yield from items
        if len(items) < SEARCH_PAGE_SIZE:
            return
        page += 1


def collect_prs_to_check(
    repo: str,
    created_since: str,
    github_token: str,
    alerted: set[str],
    max_prs: int,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Collect unalerted PRs in creation order without fetching their details."""
    pending: list[dict[str, Any]] = []
    latest_created = ""

    for summary in newly_created_prs(repo, created_since, github_token):
        if not isinstance(summary, dict):
            raise WatcherError("unexpected pull-request entry in GitHub search response")
        try:
            number = int(summary["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WatcherError("GitHub search returned a pull request without a valid number") from exc
        created_at = str(summary.get("created_at", ""))
        if not created_at:
            raise WatcherError(f"GitHub search returned PR #{number} without a creation time")

        if str(number) in alerted:
            latest_created = max(latest_created, created_at)
            print(f"{pr_status(summary)}: already alerted, skipping")
            continue
        if len(pending) >= max_prs:
            print(f"Reached limit of {max_prs} PRs; remaining PRs will be checked next run.")
            return pending, latest_created, False

        pending.append(summary)
        latest_created = max(latest_created, created_at)

    return pending, latest_created, True


def changed_files(repo: str, pr_number: int, github_token: str) -> list[str]:
    files: list[str] = []
    page = 1
    while True:
        url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/files?" + urlencode(
            {"per_page": SEARCH_PAGE_SIZE, "page": page}
        )
        batch = github_json(url, github_token)
        if not isinstance(batch, list):
            raise WatcherError(f"unexpected changed-files response for PR #{pr_number}")
        for changed_file in batch:
            if not isinstance(changed_file, dict) or not changed_file.get("filename"):
                raise WatcherError(f"unexpected changed-file entry for PR #{pr_number}")
            files.append(str(changed_file["filename"]))
        if len(batch) < SEARCH_PAGE_SIZE:
            return files
        page += 1


def pr_details(repo: str, pr_number: int, github_token: str) -> dict[str, Any]:
    detail = github_json(f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}", github_token)
    if not isinstance(detail, dict):
        raise WatcherError(f"unexpected PR response for PR #{pr_number}")
    return detail


def latest_commit_time(repo: str, pr: dict[str, Any], github_token: str) -> str:
    """Return the current head commit time, not the PR's metadata update time."""
    head = pr.get("head", {})
    head_sha = head.get("sha") if isinstance(head, dict) else ""
    if not head_sha:
        return ""

    # PR.updated_at also changes for comments and labels. The head commit is the
    # actual latest code revision on the PR.
    commit = github_json(f"{GITHUB_API}/repos/{repo}/commits/{head_sha}", github_token)
    if not isinstance(commit, dict):
        raise WatcherError(f"unexpected commit response for PR #{pr['number']}")
    details = commit.get("commit", {})
    if not isinstance(details, dict):
        return ""
    committer = details.get("committer", {})
    author = details.get("author", {})
    if isinstance(committer, dict) and committer.get("date"):
        return str(committer["date"])
    if isinstance(author, dict) and author.get("date"):
        return str(author["date"])
    return ""


# Recipient and file matching

def receiver_groups() -> dict[str, str]:
    raw_groups = require_env("RECEIVER_EMAILS")
    try:
        groups = yaml.safe_load(raw_groups) or {}
    except yaml.YAMLError as exc:
        raise WatcherError(f"RECEIVER_EMAILS is not valid YAML: {exc}") from exc
    if not isinstance(groups, dict):
        raise WatcherError("RECEIVER_EMAILS must be a YAML mapping")
    return {str(key): str(value) for key, value in groups.items()}


def resolve_recipients(reference: str, groups: dict[str, str]) -> list[str]:
    if reference.startswith("$"):
        group_name = reference[1:]
        if group_name not in groups:
            raise WatcherError(f"RECEIVER_EMAILS does not define {group_name}")
        reference = groups[group_name]
    else:
        reference = os.path.expandvars(reference)

    recipients = [address.strip() for address in reference.split(";") if address.strip()]
    if not recipients:
        raise WatcherError("an alert rule resolved to no email recipients")
    return recipients


def matches(filepath: str, pattern: str) -> bool:
    # A trailing slash means "any file below this directory". All other rules
    # use normal shell-style matching, such as drivers/**/foo.c.
    if pattern.endswith("/"):
        pattern_parts = pattern.rstrip("/").split("/")
        path_parts = filepath.split("/")
        return len(path_parts) > len(pattern_parts) and fnmatch.fnmatch(
            "/".join(path_parts[: len(pattern_parts)]), pattern.rstrip("/")
        )
    return fnmatch.fnmatch(filepath, pattern)


def fetch_yaml(url: str, yaml_cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Download and parse an external YAML file once per run and URL."""
    if url not in yaml_cache:
        for attempt in range(3):
            try:
                request = Request(url, headers={"User-Agent": "github-watcher"})
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    loaded = yaml.safe_load(response.read().decode("utf-8")) or {}
            except yaml.YAMLError as exc:
                raise WatcherError(f"invalid YAML in alert patterns from {url}: {exc}") from exc
            except HTTPError as exc:
                if exc.code not in RETRYABLE_HTTP_CODES or attempt == 2:
                    raise WatcherError(f"could not load alert patterns ({exc.code}): {url}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == 2:
                    reason = getattr(exc, "reason", str(exc))
                    raise WatcherError(f"could not load alert patterns from {url}: {reason}") from exc
            else:
                if not isinstance(loaded, dict):
                    raise WatcherError(f"alert patterns from {url} must be a YAML mapping")
                yaml_cache[url] = loaded
                return loaded
            time.sleep(2**attempt)

    return yaml_cache[url]


def compile_alert_rules(
    config: dict[str, Any], groups: dict[str, str]
) -> list[tuple[list[str], list[str]]]:
    """Resolve recipient groups and external patterns before any PR work begins."""
    yaml_cache: dict[str, dict[str, Any]] = {}
    rules: list[tuple[list[str], list[str]]] = []

    for alert in config.get("alerts", []):
        if not isinstance(alert, dict) or "emails" not in alert:
            raise WatcherError("each alert needs an emails field")
        recipients = resolve_recipients(str(alert["emails"]), groups)
        rules.append((recipients, patterns_for_alert(alert, yaml_cache)))
    return rules


def recipient_file_map(
    changed: list[str], rules: list[tuple[list[str], list[str]]]
) -> dict[str, list[str]]:
    recipient_files: defaultdict[str, set[str]] = defaultdict(set)

    for recipients, patterns in rules:
        for filepath in changed:
            if any(matches(filepath, pattern) for pattern in patterns):
                for recipient in recipients:
                    recipient_files[recipient].add(filepath)

    return {recipient: sorted(files) for recipient, files in recipient_files.items()}


def fetch_changed_files_concurrently(
    repo: str, prs: list[dict[str, Any]], github_token: str
) -> dict[int, list[str]]:
    """Fetch PR file lists in parallel; workers never email or write state."""
    if not prs:
        return {}

    files_by_pr: dict[int, list[str]] = {}
    worker_count = min(FILE_FETCH_WORKERS, len(prs))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="pr-files") as executor:
        futures = {
            executor.submit(changed_files, repo, int(pr["number"]), github_token): pr for pr in prs
        }
        for future in as_completed(futures):
            pr = futures[future]
            number = int(pr["number"])
            try:
                files_by_pr[number] = future.result()
            except Exception as exc:
                # Cancel queued work. Running workers may finish, but none can
                # send mail or update state, so this remains safe to retry.
                for pending in futures:
                    pending.cancel()
                raise WatcherError(
                    f"could not fetch changed files for PR #{number} after retries: {exc}"
                ) from exc
    return files_by_pr


def patterns_for_alert(alert: dict[str, Any], yaml_cache: dict[str, dict[str, Any]]) -> list[str]:
    if alert.get("type", "paths") == "paths":
        patterns = alert.get("patterns", [])
    elif alert.get("type") == "yaml_section":
        url = alert.get("url")
        if not url:
            raise WatcherError("yaml_section alert is missing url")
        section = fetch_yaml(url, yaml_cache).get(alert.get("section"), {})
        if not isinstance(section, dict):
            raise WatcherError(f"YAML section {alert.get('section')} must be a mapping")
        patterns = section.get(alert.get("files_key", "files"), [])
    else:
        raise WatcherError(f"unsupported alert type: {alert.get('type')}")

    if not isinstance(patterns, list):
        raise WatcherError("alert patterns must be a list")
    return [str(pattern) for pattern in patterns]


# Email delivery

def email_provider(from_email: str) -> tuple[str, str, str]:
    _, address = parseaddr(from_email)
    address = address or from_email.strip()
    domain = address.rpartition("@")[2].lower()
    if not domain:
        raise WatcherError("FROM_EMAIL must contain an email address")
    if domain in GMAIL_DOMAINS:
        return "Gmail", "smtp.gmail.com", address
    return "Resend", "smtp.resend.com", "resend"


def send_email(from_email: str, api_key: str, recipient: str, subject: str, text: str) -> None:
    provider, host, username = email_provider(from_email)
    try:
        sender = EmailSender(
            host=host,
            port=587,
            username=username,
            password=api_key,
            use_starttls=True,
        )
        sender.send(subject=subject, sender=from_email, receivers=[recipient], text=text)
    except Exception as exc:
        raise WatcherError(f"email delivery through {provider} failed for {recipient}: {exc}") from exc


def display_time(timestamp: Any) -> str:
    """Render GitHub's ISO timestamp in the short, stable format used in email."""
    if not timestamp:
        return "Unknown"
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return str(timestamp)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def pr_status(pr: dict[str, Any]) -> str:
    """Return a concise PR label for console output, including known timestamps."""
    timestamps = []
    if pr.get("created_at"):
        timestamps.append(f"created: {display_time(pr['created_at'])}")
    if pr.get("updated_at"):
        timestamps.append(f"last modified: {display_time(pr['updated_at'])}")
    dates = f" ({'; '.join(timestamps)})" if timestamps else ""
    return f"PR #{pr['number']}{dates}"


def message_for(
    repo: str, pr: dict[str, Any], files: list[str], latest_commit: str
) -> tuple[str, str]:
    number = pr["number"]
    author = pr.get("user", {}).get("login", "unknown")
    subject = f"PR #{number} touches watched files in {repo}"
    lines = [
        f"Repository: {repo}",
        f"PR: #{number}",
        f"Title: {pr.get('title', '')}",
        f"Author: {author}",
        f"Created: {display_time(pr.get('created_at'))}",
    ]
    if latest_commit:
        lines.append(f"Latest commit: {display_time(latest_commit)}")
    lines.extend([f"URL: {pr.get('html_url', '')}", "", "Matched files:", *files])
    text = "\n".join(lines)
    return subject, text


def send_pr_alerts(
    repo: str,
    pr: dict[str, Any],
    recipient_files: dict[str, list[str]],
    latest_commit: str,
    from_email: str,
    api_key: str,
) -> None:
    print(f"{pr_status(pr)}: match found — alerting {len(recipient_files)} recipient(s)")
    for recipient, files in recipient_files.items():
        subject, text = message_for(repo, pr, files, latest_commit)
        send_email(from_email, api_key, recipient, subject, text)
        print(f"  Email sent to {recipient}")


# Main workflow

def run(config_path: Path) -> None:
    # Nothing writes state while processing. This makes a failed run retryable.
    config = load_yaml_file(config_path)
    from_email = require_env("FROM_EMAIL")
    api_key = require_env("EMAIL_API_KEY")
    groups = receiver_groups()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    max_prs = 500 if github_token else 25

    try:
        lookback_days = int(config.get("initial_lookback_days", 365))
    except (TypeError, ValueError) as exc:
        raise WatcherError("initial_lookback_days must be an integer") from exc

    alerted_file, last_run_file = state_paths(config_path)
    alerted, created_since = load_state(alerted_file, last_run_file, lookback_days)
    print(f"Checking new PRs in {config['repo']} (limit: {max_prs})")
    # The cursor only advances after every file-list request has succeeded.
    pending, latest_created, completed_search = collect_prs_to_check(
        config["repo"], created_since, github_token, alerted, max_prs
    )

    if pending:
        # Rule loading is cached in memory for this run only; nothing is stored
        # or committed, and all three Zephyr sections share one YAML download.
        rules = compile_alert_rules(config, groups)
        print(f"Fetching changed files for {len(pending)} PRs with {min(FILE_FETCH_WORKERS, len(pending))} workers")
        files_by_pr = fetch_changed_files_concurrently(config["repo"], pending, github_token)
    else:
        rules = []
        files_by_pr = {}

    # Worker results can complete out of order. Alerts remain oldest-first so a
    # cursor can safely resume if a later detail, commit, or email call fails.
    for summary in pending:
        number = int(summary["number"])
        recipients = recipient_file_map(files_by_pr[number], rules)
        if not recipients:
            print(f"{pr_status(summary)}: no match")
            continue

        # Details and head-commit data are only needed for PRs that will alert.
        detail = pr_details(config["repo"], number, github_token)
        if detail.get("state") != "open":
            print(f"{pr_status(detail)}: no longer open, skipping")
            continue
        # Only mark a PR after every recipient accepted the message.
        commit_time = latest_commit_time(config["repo"], detail, github_token)
        send_pr_alerts(config["repo"], detail, recipients, commit_time, from_email, api_key)
        alerted.add(str(number))

    if completed_search:
        next_created_since = latest_created or utc_now()
    else:
        next_created_since = latest_created or created_since
    # This is the only state write. A delivery failure above exits before here,
    # leaving alerted/ untouched, so Actions has nothing to commit.
    save_state(alerted_file, last_run_file, alerted, next_created_since)
    print(f"Progress saved — creation cursor: {next_created_since}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path, help="watcher YAML configuration file")
    args = parser.parse_args()
    try:
        run(args.config)
    except WatcherError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Keep unexpected failures visible and nonzero. Because save_state() is
        # called only at the end of run(), processing errors cannot commit PRs.
        print(f"ERROR: unexpected failure: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
