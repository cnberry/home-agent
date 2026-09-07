#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "restore must run as root" >&2
  exit 1
fi
restore_in_progress=false
trap '
  if [[ "$restore_in_progress" == true ]]; then
    echo "Restore did not complete; units stopped for restore were not restarted." >&2
    echo "Resolve the failure before restarting the agent, heartbeat, and backup units." >&2
  fi
' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
restore_active_units=()
for unit in home-agent-heartbeat.timer home-agent-state-backup.timer; do
  case "$(systemctl show --property=ActiveState --value "$unit")" in
    active|activating|reloading) restore_active_units+=("$unit") ;;
  esac
done
restore_in_progress=true
systemctl stop home-agent-heartbeat.timer home-agent-state-backup.timer
for unit in home-agent-heartbeat.service home-agent-state-backup.service; do
  case "$(systemctl show --property=ActiveState --value "$unit")" in
    active|activating|reloading) restore_active_units+=("$unit") ;;
  esac
done
systemctl stop home-agent.service home-agent-heartbeat.service home-agent-state-backup.service
runuser -u home-agent-backup -- env -i HOME=/var/lib/home-agent-backup PATH=/usr/bin:/bin \
  git -C /var/lib/home-agent-backup/repo fetch origin state-backup
runuser -u home-agent-backup -- env -i HOME=/var/lib/home-agent-backup PATH=/usr/bin:/bin \
  git -C /var/lib/home-agent-backup/repo reset --hard origin/state-backup
HOME_AGENT_CONFIG=/etc/home-agent/config.toml \
  /opt/home-agent/current/venv/bin/agentctl restore
chown home-agent:home-agent-state /var/lib/home-agent/durable/*.md
chmod 0640 /var/lib/home-agent/durable/*.md
restore_in_progress=false
systemctl start home-agent.service "${restore_active_units[@]}"
echo "Durable state restored and agent restarted."
