# Home Agent Codex–Telegram Agent

Home Agent is a persistent, single-owner Telegram interface for Codex on a separate
Ubuntu/Debian or Arch/Omarchy computer. Telegram is only the transport: every agent turn is run by the stable
OpenAI Codex Python SDK and its pinned Codex runtime.

> [!WARNING]
> Codex runs with `Sandbox.full_access` and `ApprovalMode.deny_all`, equivalent to
> `codex --yolo`. The service starts as `home-agent` and has unrestricted passwordless
> sudo, so an authorized Telegram message can change the entire host as root without approval.

## Design

- One numeric Telegram owner ID, private direct messages only.
- Telegram Bot API long polling; no inbound port or Codex listener.
- One FIFO Codex turn at a time, with SQLite WAL persistence and restart recovery.
- Separate persisted Codex threads for Telegram work and heartbeat work.
- Thirty-minute host-health/task heartbeat and a daily curated-state backup.
- Root-owned releases and systemd units; agent-owned workspace, Codex home, and runtime state.
- Separate `home-agent-backup` user and repository-scoped deploy key.

The application never stores API credentials in the repository. The Telegram token is loaded
as a systemd credential, and ChatGPT authentication stays in a mode-0700 Codex home.

## Clean installation

Prerequisites are an Ubuntu/Debian or Arch/Omarchy system with systemd and outbound HTTPS, a
Telegram bot token, and the owner's numeric Telegram user ID.

```bash
git clone https://github.com/cnberry/home-agent.git
cd home-agent
git checkout <release-tag-or-reviewed-commit>
sudo ./scripts/bootstrap.sh
```

The bootstrap validates the OS and architecture, installs locked dependencies, creates both
service users, requests the token without echoing it, validates the bot, runs local ChatGPT
device-code login, and runs `agentctl doctor` before enabling the agent.

Durable-state backup is deliberately unconfigured in this public repository. Install a single
private Git repository URL at `/etc/home-agent/backup-repository`, then run
`sudo ./scripts/configure-backup.sh`. Add the printed public key as a write-enabled deploy key on
that private repository and initialize or restore its `state-backup` branch. Never point state
backup at the public Home Agent source repository.

For unattended release updates after initial setup:

```bash
git fetch --tags
git checkout <release-tag-or-reviewed-commit>
sudo ./scripts/update.sh
```

The Telegram-controlled account can use sudo to modify the host. Persistent runtime changes
should still be made through this repository so clean installs reproduce them.

## SSH smoke-test interface

After the runtime is installed and Codex authentication is complete, an administrator can submit
a one-shot task over SSH before Telegram is configured:

```bash
printf '%s\n' 'Report current host health.' \
  | ssh ADMIN@HOME_AGENT_HOST home-agent-codex
```

`home-agent-codex` starts as the `home-agent` account with full Codex access, no approval
prompts, and unrestricted passwordless sudo. This is an administrative bootstrap and diagnostic
interface, not a public listener.

For a queued turn that shares the persistent Telegram conversation and returns its reply to the
SSH caller instead of Telegram:

```bash
printf '%s\n' 'Report current host health.' \
  | ssh ADMIN@HOME_AGENT_HOST home-agent-codex-bridge
```

The bridge uses the same FIFO queue and Telegram Codex thread. Its reply is stored with the job
until returned to the caller; it does not send a duplicate Telegram message.

The default model is `gpt-5.6-luna` with `low` reasoning for responsive, cost-sensitive routine
work. Override `agent.model` and `agent.reasoning_effort` in `/etc/home-agent/config.toml` when a
task profile needs more capability.

## Telegram commands

- Plain text queues a Codex task and returns `Queued #<id>`.
- `/help` shows commands and the agent's authority.
- `/new` archives the current Telegram Codex thread.
- `/status` shows active duration, queue depth, recent success/heartbeat, and authentication.
- `/stop` interrupts the active turn; queued work remains queued.
- `/heartbeat` forces an immediate heartbeat job.
- `/retry <job-id>` explicitly retries a failed, cancelled, or uncertain job.

Messages over 12,000 characters and v1 attachments are rejected. Unauthorized accounts, bots,
groups, channels, forwarded messages, and edited messages are silently ignored.

## Durable state

Only these files are backed up to the configured private repository's `state-backup` branch:

- `tasks.md`
- `memory.md`
- `heartbeat.md`
- `backup-manifest.json`, containing hashes and schema version

An unchecked task is due immediately unless it includes `(due: YYYY-MM-DD)`. Credentials,
conversations, SQLite, logs, Codex sessions, downloaded files, and generated backups are never
included. Backup refuses symlinks, unexpected names, files over 256 KiB, and aggregate state over
1 MiB; unchanged hashes produce no commit.

See [operations](docs/OPERATIONS.md), [security](docs/SECURITY.md), and
[clean-system recovery](docs/RECOVERY.md) for the generic runbooks. Keep machine-specific
recovery steps, SSH destinations, schedules, and other deployment details in a separate private
configuration repository.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock
.venv/bin/pip install --no-build-isolation --no-deps -e .
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest
```

Runtime dependencies and the embedded Codex runtime are intentionally pinned. Refresh locks with
`pip-compile --generate-hashes` only in a reviewed dependency-update change.
