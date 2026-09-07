#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "restore must run as root" >&2
  exit 1
fi
systemctl stop home-agent.service
runuser -u home-agent-backup -- env -i HOME=/var/lib/home-agent-backup PATH=/usr/bin:/bin \
  git -C /var/lib/home-agent-backup/repo fetch origin state-backup
runuser -u home-agent-backup -- env -i HOME=/var/lib/home-agent-backup PATH=/usr/bin:/bin \
  git -C /var/lib/home-agent-backup/repo reset --hard origin/state-backup
HOME_AGENT_CONFIG=/etc/home-agent/config.toml \
  /opt/home-agent/current/venv/bin/agentctl restore
chown home-agent:home-agent-state /var/lib/home-agent/durable/*.md
chmod 0640 /var/lib/home-agent/durable/*.md
systemctl start home-agent.service
echo "Durable state restored and agent restarted."
