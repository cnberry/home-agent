# Home Agent

![A tiny one-eyed robot receives a message and flips a switch](docs/assets/home-agent-hero.png)

> **OpenClaw energy. Toaster-level ambition.**
>
> You ask. It does. Thinking not included. 😉

Okay, Codex does the thinking. Home Agent is the deliberately boring bit in the middle: a tiny,
persistent, single-owner Telegram remote control for Codex on a separate Ubuntu/Debian or
Arch/Omarchy computer.

There is no second agent framework, plugin universe, or orchestration maze. It authenticates one
owner, queues each message, survives restarts, hands the work to the stable OpenAI Codex Python
SDK, and brings the answer back. That's pretty much the trick.

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

## Telegram bot setup

Complete this before installing Home Agent on its dedicated Linux host. The configured owner
can ask the agent to use root access, so use your own secured Telegram account and a dedicated
bot, not a shared bot or group. Keep deployment-specific values out of this public repository.

### 1. Create the bot and store its credentials

1. Open the official [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot` and follow the prompts for a display name and a unique bot username.
3. Create a 1Password item for this deployment. Store the bot token in a concealed password
   field and record the bot username alongside it. Add your numeric owner ID after the next step.
4. In BotFather, use `/setjoingroups` to disable adding this bot to groups. Leave inline mode off;
   Home Agent only supports private direct messages.
5. Open your new bot's private chat from the Telegram account that will control Home Agent and
   press **Start**. No reply is expected until Home Agent is installed and running.

Treat the token as a password. Do not put it in shell commands, browser URLs, source files,
screenshots, issues, or chat with an assistant. If exposed, replace it through BotFather and
update both 1Password and the host credential. Telegram's
[official creation guide](https://core.telegram.org/bots/tutorial#obtain-your-bot-token)
explains the BotFather flow; [bot settings](https://core.telegram.org/bots/features#edit-settings)
are documented separately.

### 2. Find your numeric owner ID through your own bot

Home Agent needs your Telegram **user ID**, not your username, phone number, bot ID, or the
number before the colon in the bot token. Do not substitute the example ID from the config.

Run the following in an interactive Linux terminal with `python3` available, either on your
administrator workstation or on the future agent host. It uses Python's standard library,
prompts for the token without echoing it, and prints a one-time message to send to your bot.
It does not install or start Home Agent.

Use this only for initial setup of your dedicated bot: it consumes setup updates. No other
program may poll the same bot while it runs. For an existing bot, stop its old service first
and resolve pending work before using this procedure. A configured webhook must also be
removed intentionally before switching to polling; this procedure refuses to remove it for you.

```bash
python3 - <<'PY'
import getpass
import json
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

token = getpass.getpass("Telegram bot token from 1Password: ").strip()
if ":" not in token:
    raise SystemExit("The token is empty or malformed.")

def api(method, **parameters):
    url = "https://api.telegram.org/bot" + token + "/" + method
    try:
        with urlopen(url, data=urlencode(parameters).encode(), timeout=35) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise SystemExit(
            f"Telegram HTTP {error.code}; check the token, webhook, and other polling processes."
        ) from None
    except (URLError, TimeoutError, ValueError):
        raise SystemExit("Telegram request failed; check outbound HTTPS and try again.") from None
    if not payload.get("ok"):
        raise SystemExit("Telegram rejected the request; no credentials were printed.")
    return payload["result"]

identity = api("getMe")
print("Bot: @" + identity["username"])
if api("getWebhookInfo")["url"]:
    raise SystemExit("This bot has a webhook. Resolve its existing deployment before continuing.")

challenge = "home-agent-setup-" + secrets.token_hex(6)
print("From your intended owner account, send this exact text in that bot's private chat:")
print(challenge)
deadline = time.monotonic() + 180
offset = 0
while time.monotonic() < deadline:
    updates = api("getUpdates", offset=offset, timeout=30, allowed_updates='["message"]')
    for update in updates:
        offset = update["update_id"] + 1
        message = update.get("message", {})
        sender = message.get("from", {})
        if (message.get("chat", {}).get("type") == "private"
                and message.get("text") == challenge and not sender.get("is_bot", True)):
            # Acknowledge the setup message so it cannot become a task after installation.
            api("getUpdates", offset=offset, timeout=0, allowed_updates='["message"]')
            print(json.dumps({"owner_id": sender["id"], "username": sender.get("username")}))
            raise SystemExit(0)
raise SystemExit("No matching private message received. Check the bot/account and rerun.")
PY
```

Confirm the printed bot username and the account you used, then record `owner_id` in 1Password.
Only send the setup message while this helper runs; do not send real tasks until installation
finishes. The helper uses Telegram's documented
[long-polling and acknowledgement API](https://core.telegram.org/bots/api#getupdates).

The installed `agentctl telegram-id` command is a separate administrative helper that needs an
existing config and token. It reports the next sender, so it is not a substitute for the
one-time-message identity check above and must not compete with a running gateway.

### 3. Supply the values during installation

Follow **Clean installation** below on the actual Home Agent host, not automatically on your
administrator workstation. When bootstrap prompts, enter the numeric owner ID and paste the
token from 1Password into its hidden prompt. Complete the prompted Codex device login locally.

Bootstrap writes the owner ID to `/etc/home-agent/config.toml` and stores the token in the
root-owned, mode `0600` file `/etc/home-agent/credentials/telegram-token`. The service receives
the token through systemd credentials; do not loosen the file permissions to let the agent
read it directly. An existing configuration is preserved, so rerunning bootstrap is not a way
to change its owner ID.

Only one host should poll this bot. Keep the old deployment stopped when bringing up a
replacement host. No webhook, public URL, port forwarding, or inbound firewall opening is needed.

After installation, send `/start` or `/help`, then `/status`, from the owner's private chat.
Continue with the [live Telegram acceptance checklist](docs/RECOVERY.md#telegram-end-to-end-checklist).
That checklist is pending deployment; writing these instructions does not validate a live bot.

### Troubleshooting the first connection

- `401` or token validation failure: check the token in 1Password, the intended bot, and whether
  BotFather has replaced its token. Never paste the failing token into logs or an issue.
- `409` or conflicting polling/webhook errors: stop the old host and all ID-discovery helpers;
  confirm no webhook-based deployment still owns this bot.
- No private reply: press **Start**, confirm the configured numeric owner ID belongs to the
  account you are messaging from, and check `sudo systemctl status home-agent.service`.
- `/status` replies but tasks do not run: investigate Codex authentication with the
  [operations guide](docs/OPERATIONS.md); Telegram setup and Codex login are separate concerns.

## Clean installation

Prerequisites are an Ubuntu/Debian or Arch/Omarchy system with systemd and outbound HTTPS, plus
the bot token and numeric owner ID obtained in [Telegram bot setup](#telegram-bot-setup).

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

## Independent panel queue

`agentctl panel-run` runs a dedicated panel worker using the same pinned Codex SDK,
workspace, login and tools. `agentctl panel-bridge` accepts a prompt on stdin and
returns its response. Both derive a separate `.panel.sqlite3` database alongside
the configured main database. Panel jobs and Codex conversation IDs never enter
the Telegram/heartbeat queue. One panel turn runs at a time; the independent
workers can operate concurrently and share the host/workspace.

Panel responses use concise plain-text instructions for a small display. The panel
worker does not load a Telegram token or construct a Telegram notifier. The existing
interactive job/thread label is reused only inside the separate panel database.
Restart recovery and uncertain-action no-replay behavior are shared with Home Agent.
A process lock prevents two panel workers from consuming the same queue.

A deployment must authenticate access to `panel-bridge`; it is an administrative
interface with the same Home Agent authority. Private socket/gateway configuration
and device UI belong in the client deployment repository. No network listener is
added by these CLI commands.

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

## Daily improvement loop

Installation enables a midnight cron job that reviews private timestamped interactions and can
ship tested improvements through PR → passing CI → merge → release → deploy. The reviewer runs
separately from the Telegram queue and considers code, prompts, model choice, reasoning effort,
context, tools and transport latency. It preserves failed attempts and chooses a measured NOOP
when there is no justified change. Configure GitHub publishing access for the service account.
See [interaction logging and nightly operations](docs/OPTIMIZATION.md) for privacy, configuration,
report-only verification, deployment rollback, and pausing the schedule.
