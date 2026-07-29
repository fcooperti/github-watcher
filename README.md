# github-watcher

Watches pull requests on repositories you don't own and sends email alerts when PRs touch files you care about. Runs on a 30-minute schedule via GitHub Actions.

---

## Getting started

### Prerequisites

- A GitHub account to host this repository
- A [Resend](https://resend.com) account with a verified sending domain and an API key

### Initial setup

**1. Fork or clone this repository into your own GitHub account.**

If creating from scratch, make sure the repo is set to **private** if your watched file lists or email addresses are sensitive.

**2. Enable GitHub Actions.**

Go to the **Actions** tab in your repository. If prompted, click **I understand my workflows, go ahead and enable them**.

**3. Add your secrets.**

Go to **Settings > Secrets and variables > Actions > New repository secret** and add the secrets for each repository you want to watch. See the [Secrets](#secrets) section for naming conventions and the required format.

**4. Push your config and workflow files.**

Ensure your `configs/<repo-name>-config.yml` and `.github/workflows/<repo-name>-workflow.yml` files are committed and pushed to the `main` branch. The scheduler only picks up workflows on the default branch.

**5. Test manually.**

Go to **Actions**, select your workflow from the left sidebar, and click **Run workflow > Run workflow**. Check the run logs to confirm it completes without errors.

After that, the workflow runs automatically every 30 minutes.

---

## How it works

Each watched repository gets:
- A config file in `configs/` that defines what to watch and who to notify
- A workflow file in `.github/workflows/` that runs the check on a schedule
- A set of GitHub secrets scoped to that repository

The shared script `scripts/check-prs.sh` does the actual work — it polls the GitHub API for new and recently updated PRs, checks changed files against your configured patterns, and sends email alerts via [Resend](https://resend.com).

A file named `alerted-<config-name>.txt` is committed back to the repo after each run to prevent duplicate alerts across runs.

---

## Repository layout

```
configs/
  zephyr-upstream-config.yml     # one config per watched repo
.github/workflows/
  zephyr-upstream-workflow.yml   # one workflow per watched repo
scripts/
  check-prs.sh                   # shared, not modified per repo
alerted/
  zephyr-upstream-alerted.txt    # auto-generated, do not edit
```

---

## Secrets

Each watched repository uses its own set of secrets, prefixed with the repo identifier. For the upstream Zephyr watcher the secrets are:

| Secret | Description |
|---|---|
| `ZEPHYR_UPSTREAM_RESEND_API_KEY` | Resend API key used to send emails |
| `ZEPHYR_UPSTREAM_FROM_EMAIL` | Sender address (must be a verified Resend domain) |
| `ZEPHYR_UPSTREAM_RECEIVER_EMAILS` | YAML block mapping group keys to recipient lists (see format below) |

### RECEIVER_EMAILS format

The `RECEIVER_EMAILS` secret is a YAML block that maps group name keys to semicolon-separated email lists. The group name keys must match what is referenced in `emails:` fields in the config file.

```yaml
TI_SIMPLELINK_EMAILS: alice@example.com; bob@example.com
MSPM_EMAILS: carol@example.com
```

To add a new recipient group, add a new key/value line here and reference it in the config.

---

## Config file format

Each config file lives in `configs/` and follows this structure:

```yaml
# GitHub repo to monitor (owner/repo)
repo: "owner/repo-name"

alerts:
  # Alert type 1 — static path patterns
  - type: paths
    emails: $GROUP_KEY        # references a key in RECEIVER_EMAILS secret
    patterns:
      - "drivers/foo/**"      # glob pattern matched against changed file paths
      - "boards/bar/**"
      - "include/some/file.h"

  # Alert type 2 — pull file patterns from a section of an external YAML file
  - type: yaml_section
    url: "https://raw.githubusercontent.com/.../MAINTAINERS.yml"
    section: "Section Name"   # top-level key in the external YAML
    files_key: "files"        # key within that section that holds the file list
    emails: $GROUP_KEY
```

Both alert types can be mixed in the same config. The `emails:` value must start with `$` followed by a key defined in the `RECEIVER_EMAILS` secret.

---

## Adding a new watched repository

### 1. Create the config file

Copy an existing config as a starting point:

```
configs/<repo-name>-config.yml
```

Set `repo:` to the target `owner/repo` and define your `alerts:` entries.

### 2. Create the workflow file

Copy an existing workflow as a starting point:

```
.github/workflows/<repo-name>-workflow.yml
```

Update:
- `name:` — human-readable workflow name shown in the Actions tab
- `env:` — change the secret references to use the new `<REPO_NAME>_*` prefix
- `run:` — point to the new config file path
- The commit message label at the bottom

Example workflow `env:` block for a repo named `linux-kernel`:

```yaml
env:
  RESEND_API_KEY: ${{ secrets.LINUX_KERNEL_RESEND_API_KEY }}
  RECEIVER_EMAILS: ${{ secrets.LINUX_KERNEL_RECEIVER_EMAILS }}
  FROM_EMAIL: ${{ secrets.LINUX_KERNEL_FROM_EMAIL }}
```

### 3. Add the GitHub secrets

In this repository's **Settings > Secrets and variables > Actions**, add:

- `<REPO_NAME>_RESEND_API_KEY`
- `<REPO_NAME>_FROM_EMAIL`
- `<REPO_NAME>_RECEIVER_EMAILS` (using the YAML format described above)

### 4. Test it

Trigger the workflow manually from the **Actions** tab using the `workflow_dispatch` event to confirm it runs without errors before waiting for the next scheduled run.

---

## Deduplication

Once a PR number has triggered an alert it is written to `alerted/<repo-name>-alerted.txt` and will not trigger another alert, even if the PR is updated again later. This file is committed automatically by the workflow after each run. The `alerted/` folder is tracked in git — do not edit these files manually.
