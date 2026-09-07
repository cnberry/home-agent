# Operations

## Service health

```bash
sudo systemctl status home-agent.service
sudo systemctl list-timers 'home-agent-*'
sudo journalctl -u home-agent.service --since today
sudo -u home-agent /opt/home-agent/current/venv/bin/agentctl \
  --config /etc/home-agent/config.toml doctor --skip-telegram
sudo ./scripts/diagnose.sh
```

The normal outbound connections are Telegram Bot API HTTPS, OpenAI/Codex HTTPS, and GitHub SSH
from the backup account. The host exposes no listener for the agent.

## Authentication recovery

When `/status` reports degraded authentication, queued jobs are retained but the worker does not
launch them. Authenticate locally; never send a code, cookie, token, or credential through
Telegram.

```bash
sudo -u home-agent env CODEX_HOME=/var/lib/home-agent/codex-home \
  /opt/home-agent/current/venv/bin/agentctl \
  --config /etc/home-agent/config.toml auth
sudo systemctl restart home-agent.service
```

Successful authentication clears the degraded flag and queued work resumes.

## Diagnostics

```bash
sudo systemctl --failed
df -h
free -h
uptime
cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null
sudo ./scripts/update.sh
```

Linux thermal readings are usually millidegrees Celsius; divide values such as `65000` by 1000.
On hosts with lm-sensors installed, `sensors` provides a friendlier view.

The runtime database is `/var/lib/home-agent/runtime/runtime.sqlite3`. It is operational
state, not a backup artifact. Do not edit it while the service is running.

## Failure semantics

- A clearly transient failure before a Codex turn begins is retried with bounded backoff.
- A failure after a turn may have started is marked `uncertain` and never replayed automatically.
- A 45-minute turn is interrupted and marked `uncertain`.
- Restart converts any `running` job to `uncertain`; queued work is retained.
- `/stop` marks the active turn cancelled and never replays it.
- Use `/retry <id>` only after deciding that repeating possible side effects is safe.

## Updates and removal

Updates are explicit, root-run deployments from a reviewed tag or commit. The previous release
remains in `/opt/home-agent/releases` for manual rollback by repointing `current` and
restarting the service.

```bash
sudo ./scripts/uninstall.sh
```

This removes software and units but preserves configuration, credentials, and state. The
destructive `--purge` option permanently removes all Home Agent users, credentials, runtime, and
durable state.
