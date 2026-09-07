#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "diagnose must run as root" >&2
  exit 1
fi

echo "Home Agent service state"
systemctl --no-pager --full status home-agent.service || true
systemctl --no-pager list-timers 'home-agent-*' || true

echo
echo "Service-user boundary"
id home-agent
id home-agent-backup
getent passwd home-agent home-agent-backup

echo
echo "Agent diagnostics"
runuser -u home-agent -- env -i \
  HOME=/var/lib/home-agent \
  CODEX_HOME=/var/lib/home-agent/codex-home \
  PATH=/opt/home-agent/current/venv/bin:/usr/bin:/bin \
  /opt/home-agent/current/venv/bin/agentctl \
  --config /etc/home-agent/config.toml doctor --skip-telegram || true

echo
echo "Host health"
systemctl --failed --no-pager || true
df -h / /srv/home-agent /var/lib/home-agent
free -h
uptime

echo
echo "Recent sanitized application events"
journalctl -u home-agent.service -n 100 --no-pager || true
