#!/usr/bin/env python3
import json, os, sys, fnmatch, yaml
from collections import defaultdict
from urllib.request import urlopen

config_file = sys.argv[1]
changed = sys.stdin.read().strip().splitlines()

with open(config_file) as f:
    config = yaml.safe_load(f)

receiver_emails = yaml.safe_load(os.environ.get('RECEIVER_EMAILS', '') or '') or {}


def resolve_emails(ref):
    if ref.startswith('$'):
        key = ref[1:]
        if key in receiver_emails:
            return receiver_emails[key]
    return os.path.expandvars(ref)


def matches_pattern(filepath, pattern):
    if pattern.endswith('/'):
        pattern_parts = pattern.rstrip('/').split('/')
        path_parts = filepath.split('/')
        if len(path_parts) <= len(pattern_parts):
            return False
        dir_prefix = '/'.join(path_parts[:len(pattern_parts)])
        return fnmatch.fnmatch(dir_prefix, pattern.rstrip('/'))
    return fnmatch.fnmatch(filepath, pattern)


url_cache = {}


def fetch_yaml(url):
    if url not in url_cache:
        with urlopen(url) as r:
            url_cache[url] = yaml.safe_load(r.read().decode())
    return url_cache[url]


email_files = defaultdict(set)

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
