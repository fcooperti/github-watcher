# github-watcher

A scheduled watcher for newly created PRs. It emails the owners of configured file patterns, once per PR.

```text
scripts/check_prs.py                        # script behavior
configs/zephyr-upstream-config.yml          # repository and watched paths
.github/workflows/zephyr-upstream-workflow.yml # schedule, secrets, commit
alerted/zephyr-upstream-alerted.txt          # alerted PR numbers
alerted/zephyr-upstream-last-run.txt         # creation-time cursor
```

## GitHub Actions workflow

The workflow is responsible for scheduling the script, installing its two dependencies, providing secrets, and committing changed files under `alerted/` after a successful run. It runs at minutes 7 and 37 of every hour (every 30 minutes), avoiding the busy start of the hour. It needs `contents: write` permission for that final commit. It also sets `PYTHONDONTWRITEBYTECODE=1`, so Python does not create `__pycache__` files in the checkout.

For the Zephyr watcher, configure these repository secrets:

| Secret | Required | Purpose |
|---|---:|---|
| `ZEPHYR_UPSTREAM_EMAIL_API_KEY` | Yes | Resend API key or Gmail SMTP app password. Store the raw secret value only, with no YAML key name, quotes, or `Bearer` prefix. |
| `ZEPHYR_UPSTREAM_FROM_EMAIL` | Yes | Sender address, preferably a plain email address such as `alerts@example.com`. |
| `ZEPHYR_UPSTREAM_RECEIVER_EMAILS` | Yes | YAML recipient-group mapping. See the required format below. |
| `ZEPHYR_UPSTREAM_GITHUB_TOKEN` | No | GitHub token for higher API capacity. Store only the raw token value, not `Bearer <token>`. |

The workflow maps those repository-specific secrets to the generic names the script expects:

```yaml
EMAIL_API_KEY: ${{ secrets.ZEPHYR_UPSTREAM_EMAIL_API_KEY }}
FROM_EMAIL: ${{ secrets.ZEPHYR_UPSTREAM_FROM_EMAIL }}
RECEIVER_EMAILS: ${{ secrets.ZEPHYR_UPSTREAM_RECEIVER_EMAILS }}
GITHUB_TOKEN: ${{ secrets.ZEPHYR_UPSTREAM_GITHUB_TOKEN }}
```

To add another watcher, copy the workflow and config, use a different config filename, and give it its own secret names. The filename determines the `alerted/<name>-*.txt` state files.

## Secret value format

GitHub secret values should be entered as the value only. Do not include shell syntax such as `KEY=value`, surrounding quotes, or YAML block markers unless those characters are part of the actual secret.

`ZEPHYR_UPSTREAM_EMAIL_API_KEY` maps to `EMAIL_API_KEY`. Its value is sent to SMTP as the password:

```text
re_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

For Gmail, use a Gmail app password instead. If the provider displays the app password in spaced groups for readability, remove those grouping spaces unless the provider explicitly says they are part of the password.

`ZEPHYR_UPSTREAM_FROM_EMAIL` maps to `FROM_EMAIL`. The domain selects the SMTP provider:

```text
alerts@example.com
```

Use a Gmail or Googlemail address to send through Gmail SMTP. Any other domain sends through Resend SMTP, so that sender address must be valid for the Resend account.

`ZEPHYR_UPSTREAM_RECEIVER_EMAILS` maps to `RECEIVER_EMAILS`. A single secret supports multiple recipient groups because its value is parsed as YAML mapping text. Group names do not include the leading `$`; the watcher config adds `$` when referencing the group. Recipient addresses inside each group are separated with semicolons:

```yaml
TI_GENERAL_LIST: alice@example.com; bob@example.com
CUSTOM_EMAILS: carla@example.com
RELEASE_REVIEWERS: dana@example.com; erin@example.com; frank@example.com
```

Any alert rule can reference one group from that same secret:

```yaml
emails: $TI_GENERAL_LIST
```

For multiple recipients in one group, keep them in one YAML value and separate them with semicolons. The script splits recipient lists only on semicolons.

`ZEPHYR_UPSTREAM_GITHUB_TOKEN` maps to `GITHUB_TOKEN`. It is optional. If set, store the raw GitHub token only:

```text
github_pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Script

Run the script from the repository root:

```bash
python -m pip install PyYAML redmail
python scripts/check_prs.py configs/zephyr-upstream-config.yml
```

The script needs these environment variables:

| Variable | Required | Purpose |
|---|---:|---|
| `EMAIL_API_KEY` | Yes | SMTP password: a Resend API key or Gmail app password. |
| `FROM_EMAIL` | Yes | Sender address used for provider detection. |
| `RECEIVER_EMAILS` | Yes | YAML mapping referenced by the watcher config. |
| `GITHUB_TOKEN` | No | GitHub token for a higher API limit. |

Email delivery uses Redmail. The sender address chooses the provider automatically:

- `sender@gmail.com` or `sender@googlemail.com` uses Gmail SMTP.
- Any other sender domain uses Resend SMTP. This lets a verified custom Resend domain work without an extra provider setting.

For Gmail, set `EMAIL_API_KEY` to the SMTP app password/auth secret. For Resend, set it to the Resend API key.

`RECEIVER_EMAILS` is a YAML mapping used by `$GROUP` references in a watcher config:

```yaml
TI_GENERAL_LIST: alice@example.com; bob@example.com
```

Watcher configs reference a group as `emails: $TI_GENERAL_LIST`. They may contain direct path patterns or obtain patterns from a section of an external YAML file; [zephyr-upstream-config.yml](/configs/zephyr-upstream-config.yml) shows both forms.

The script queries only PRs created after the saved cursor. A later update to an existing PR does not send another email. Each alert includes the PR creation time and the current head commit's time, so comments or label changes are not presented as code changes.

During a run, each external YAML URL is downloaded once and held only in memory; it is never saved or committed. Changed-file lists are fetched with up to eight concurrent workers. If any GitHub or YAML fetch exhausts its retries, the script stops before sending email or changing `alerted/` state.

For each new matching PR, it sends all required messages first. It records the PR number in `alerted/*.txt` only after every send succeeds. If a send fails, it prints an error, exits nonzero, and writes no state; the workflow therefore commits nothing and retries that PR next run.
