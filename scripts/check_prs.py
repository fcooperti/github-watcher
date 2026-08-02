#!/usr/bin/env python3
"""Alert on newly created GitHub PRs whose files match a watcher config.

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


class WatcherError(RuntimeError):
    """A configuration, GitHub, or email delivery error."""


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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def save_state(alerted_file: Path, last_run_file: Path, alerted: set[str], created_since: str) -> None:
    sorted_prs = sorted(alerted, key=int)
    atomic_write(alerted_file, "\n".join(sorted_prs) + ("\n" if sorted_prs else ""))
    atomic_write(last_run_file, f"created_since={created_since}\n")


def github_json(url: str, github_token: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-watcher",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    for attempt in range(3):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise WatcherError(f"GitHub API request failed ({exc.code}): {url}") from exc
        except URLError as exc:
            if attempt == 2:
                raise WatcherError(f"could not reach GitHub API: {exc.reason}") from exc
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
        items = result.get("items", [])
        yield from items
        if len(items) < SEARCH_PAGE_SIZE:
            return
        page += 1


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
        files.extend(file["filename"] for file in batch if file.get("filename"))
        if len(batch) < SEARCH_PAGE_SIZE:
            return files
        page += 1


def pr_details(repo: str, pr_number: int, github_token: str) -> dict[str, Any]:
    detail = github_json(f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}", github_token)
    if not isinstance(detail, dict):
        raise WatcherError(f"unexpected PR response for PR #{pr_number}")
    return detail


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
    if pattern.endswith("/"):
        pattern_parts = pattern.rstrip("/").split("/")
        path_parts = filepath.split("/")
        return len(path_parts) > len(pattern_parts) and fnmatch.fnmatch(
            "/".join(path_parts[: len(pattern_parts)]), pattern.rstrip("/")
        )
    return fnmatch.fnmatch(filepath, pattern)


def patterns_for_alert(alert: dict[str, Any], yaml_cache: dict[str, dict[str, Any]]) -> list[str]:
    if alert.get("type", "paths") == "paths":
        patterns = alert.get("patterns", [])
    elif alert.get("type") == "yaml_section":
        url = alert.get("url")
        if not url:
            raise WatcherError("yaml_section alert is missing url")
        if url not in yaml_cache:
            try:
                with urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    yaml_cache[url] = yaml.safe_load(response.read().decode("utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise WatcherError(f"could not load alert patterns from {url}: {exc}") from exc
        section = yaml_cache[url].get(alert.get("section"), {})
        patterns = section.get(alert.get("files_key", "files"), [])
    else:
        raise WatcherError(f"unsupported alert type: {alert.get('type')}")

    if not isinstance(patterns, list):
        raise WatcherError("alert patterns must be a list")
    return [str(pattern) for pattern in patterns]


def recipient_file_map(
    changed: list[str], config: dict[str, Any], groups: dict[str, str]
) -> dict[str, list[str]]:
    recipient_files: defaultdict[str, set[str]] = defaultdict(set)
    yaml_cache: dict[str, dict[str, Any]] = {}

    for alert in config.get("alerts", []):
        if not isinstance(alert, dict) or "emails" not in alert:
            raise WatcherError("each alert needs an emails field")
        recipients = resolve_recipients(str(alert["emails"]), groups)
        patterns = patterns_for_alert(alert, yaml_cache)
        for filepath in changed:
            if any(matches(filepath, pattern) for pattern in patterns):
                for recipient in recipients:
                    recipient_files[recipient].add(filepath)

    return {recipient: sorted(files) for recipient, files in recipient_files.items()}


def email_provider(from_email: str) -> tuple[str, str, str]:
    _, address = parseaddr(from_email)
    address = address or from_email.strip()
    domain = address.rpartition("@")[2].lower()
    if not domain:
        raise WatcherError("FROM_EMAIL must contain an email address")
    if domain in {"gmail.com", "googlemail.com"}:
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


def message_for(pr: dict[str, Any], files: list[str]) -> tuple[str, str]:
    number = pr["number"]
    author = pr.get("user", {}).get("login", "unknown")
    subject = f"PR #{number} touches watched files"
    text = "\n".join(
        [
            f"PR: #{number}",
            f"Title: {pr.get('title', '')}",
            f"Author: {author}",
            f"URL: {pr.get('html_url', '')}",
            "",
            "Matched files:",
            *files,
        ]
    )
    return subject, text


def run(config_path: Path) -> None:
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
    latest_created = ""
    processed = 0
    completed_search = True

    print(f"Checking new PRs in {config['repo']} (limit: {max_prs})")
    for summary in newly_created_prs(config["repo"], created_since, github_token):
        if processed >= max_prs:
            completed_search = False
            print(f"Reached limit of {max_prs} PRs; remaining PRs will be checked next run.")
            break

        number = int(summary["number"])
        created_at = summary.get("created_at", "")
        if str(number) in alerted:
            latest_created = max(latest_created, created_at)
            print(f"PR #{number} already alerted, skipping")
            continue

        processed += 1
        detail = pr_details(config["repo"], number, github_token)
        latest_created = max(latest_created, created_at)
        if detail.get("state") != "open":
            print(f"PR #{number} is no longer open, skipping")
            continue

        recipients = recipient_file_map(changed_files(config["repo"], number, github_token), config, groups)
        if not recipients:
            print(f"PR #{number}: no match")
            continue

        print(f"PR #{number}: match found — alerting {len(recipients)} recipient(s)")
        for recipient, files in recipients.items():
            subject, text = message_for(detail, files)
            send_email(from_email, api_key, recipient, subject, text)
            print(f"  Email sent to {recipient}")

        # Only mark a PR after every recipient accepted the message.
        alerted.add(str(number))

    if completed_search:
        next_created_since = latest_created or utc_now()
    else:
        next_created_since = latest_created or created_since
    # State is written only after the entire run succeeds. A delivery failure above
    # exits before this point, leaving alerted/ untouched and uncommitted.
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
