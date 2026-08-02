# github-watcher

A scheduled GitHub Actions watcher for newly created PRs. It emails the owners of configured file patterns, once per PR.

The implementation is deliberately small: one Python script, one watcher config, one workflow, and text state files.

```text
scripts/check_prs.py                     # all runtime behavior
configs/zephyr-upstream-config.yml       # repository and watched paths
.github/workflows/zephyr-upstream-workflow.yml
alerted/zephyr-upstream-alerted.txt       # alerted PR numbers, one per line
alerted/zephyr-upstream-last-run.txt      # new-PR creation cursor
```

## Runtime configuration

The workflow supplies four environment variables:

```yaml
EMAIL_API_KEY: ${{ secrets.ZEPHYR_UPSTREAM_EMAIL_API_KEY }}
FROM_EMAIL: ${{ secrets.ZEPHYR_UPSTREAM_FROM_EMAIL }}
RECEIVER_EMAILS: ${{ secrets.ZEPHYR_UPSTREAM_RECEIVER_EMAILS }}
GITHUB_TOKEN: ${{ secrets.ZEPHYR_UPSTREAM_GITHUB_TOKEN }}
```

`EMAIL_API_KEY` is used as the SMTP password. `GITHUB_TOKEN` is optional, but increases the GitHub API limit.

Email delivery uses Redmail. The sender address chooses the provider automatically:

- `sender@gmail.com` or `sender@googlemail.com` uses Gmail SMTP.
- Any other sender domain uses Resend SMTP. This lets a verified custom Resend domain work without an extra provider setting.

For Gmail, set `EMAIL_API_KEY` to the SMTP app password/auth secret. For Resend, set it to the Resend API key.

`RECEIVER_EMAILS` is a YAML mapping used by `$GROUP` references in a watcher config:

```yaml
TI_GENERAL_LIST: alice@example.com; bob@example.com
```

## Behavior

The script queries only PRs created after the saved cursor. A later update to an existing PR does not send another email.

For each new matching PR, it sends all required messages first. It records the PR number in `alerted/*.txt` only after every send succeeds. If a send fails, it prints an error, exits nonzero, and writes no state; the workflow therefore commits nothing and retries that PR next run.

Run locally after installing dependencies:

```bash
python -m pip install PyYAML redmail
python scripts/check_prs.py configs/zephyr-upstream-config.yml
```
