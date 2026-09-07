#!/usr/bin/env bash
set -euo pipefail

old_agent="strawberry-agent"
new_agent="home-agent"
old_backup="strawberry-backup"
new_backup="home-agent-backup"
old_state_group="strawberry-state"
new_state_group="home-agent-state"

if [[ ${EUID} -ne 0 ]]; then
  echo "migration must run as root" >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_root=$(cd -- "$script_dir/.." && pwd)

rename_group() {
  local old_name=$1
  local new_name=$2
  if getent group "$old_name" >/dev/null; then
    if getent group "$new_name" >/dev/null; then
      echo "Both groups $old_name and $new_name exist; refusing to merge them." >&2
      exit 1
    fi
    groupmod --new-name "$new_name" "$old_name"
  elif ! getent group "$new_name" >/dev/null; then
    echo "Neither group $old_name nor $new_name exists; run the legacy installation first." >&2
    exit 1
  fi
}

rename_user() {
  local old_name=$1
  local new_name=$2
  local new_home=$3
  if id "$old_name" >/dev/null 2>&1; then
    if id "$new_name" >/dev/null 2>&1; then
      echo "Both users $old_name and $new_name exist; refusing to merge them." >&2
      exit 1
    fi
    usermod --login "$new_name" "$old_name"
  elif ! id "$new_name" >/dev/null 2>&1; then
    echo "Neither user $old_name nor $new_name exists; run the legacy installation first." >&2
    exit 1
  fi
  usermod --home "$new_home" "$new_name"
}

move_and_link() {
  local old_path=$1
  local new_path=$2
  if [[ -L "$old_path" ]]; then
    if [[ $(readlink -f -- "$old_path") != "$new_path" ]]; then
      echo "$old_path is an unexpected symlink; refusing to replace it." >&2
      exit 1
    fi
  elif [[ -e "$old_path" ]]; then
    if [[ -e "$new_path" || -L "$new_path" ]]; then
      echo "Both $old_path and $new_path exist; refusing to merge them." >&2
      exit 1
    fi
    mv -- "$old_path" "$new_path"
    ln -s "$new_path" "$old_path"
  elif [[ -e "$new_path" ]]; then
    ln -s "$new_path" "$old_path"
  else
    echo "Neither $old_path nor $new_path exists; legacy state is incomplete." >&2
    exit 1
  fi
}

set_agent_setting() {
  local key=$1
  local value=$2
  local config=/etc/home-agent/config.toml
  if grep -Eq "^${key}[[:space:]]*=" "$config"; then
    sed -i -E "s|^${key}[[:space:]]*=.*|${key} = \"${value}\"|" "$config"
  else
    sed -i "/^\[agent\]$/a ${key} = \"${value}\"" "$config"
  fi
}

legacy_units=(
  strawberry-agent.service
  strawberry-heartbeat.service
  strawberry-heartbeat.timer
  strawberry-state-backup.service
  strawberry-state-backup.timer
)

systemctl disable --now "${legacy_units[@]}" 2>/dev/null || true
systemctl stop home-agent.service home-agent-heartbeat.service \
  home-agent-heartbeat.timer home-agent-state-backup.service \
  home-agent-state-backup.timer 2>/dev/null || true

if id "$old_agent" >/dev/null 2>&1 && pgrep -u "$old_agent" >/dev/null; then
  echo "$old_agent still has running processes; stop them before migration." >&2
  exit 1
fi
if id "$old_backup" >/dev/null 2>&1 && pgrep -u "$old_backup" >/dev/null; then
  echo "$old_backup still has running processes; stop them before migration." >&2
  exit 1
fi

rename_group "$old_state_group" "$new_state_group"
rename_group "$old_agent" "$new_agent"
rename_group "$old_backup" "$new_backup"
rename_user "$old_agent" "$new_agent" /var/lib/home-agent
rename_user "$old_backup" "$new_backup" /var/lib/home-agent-backup
usermod -a -G "$new_state_group" "$new_agent"
usermod -a -G "$new_state_group" "$new_backup"

move_and_link /opt/strawberry-agent /opt/home-agent
move_and_link /srv/strawberry-agent /srv/home-agent
move_and_link /var/lib/strawberry-agent /var/lib/home-agent
move_and_link /var/lib/strawberry-agent-backup /var/lib/home-agent-backup
move_and_link /etc/strawberry-agent /etc/home-agent

config=/etc/home-agent/config.toml
sed -i \
  -e 's|/opt/strawberry-agent|/opt/home-agent|g' \
  -e 's|/srv/strawberry-agent|/srv/home-agent|g' \
  -e 's|/var/lib/strawberry-agent-backup|/var/lib/home-agent-backup|g' \
  -e 's|/var/lib/strawberry-agent|/var/lib/home-agent|g' \
  -e 's|/etc/strawberry-agent|/etc/home-agent|g' \
  "$config"
set_agent_setting model gpt-5.6-luna
set_agent_setting reasoning_effort low

backup_checkout=/var/lib/home-agent-backup/repo
"$source_root/scripts/bootstrap.sh" --non-interactive --skip-auth

ln -sfn /usr/local/bin/home-agent-codex /usr/local/bin/strawberry-codex
ln -sfn /usr/local/bin/home-agent-codex-bridge /usr/local/bin/strawberry-codex-bridge

rm -f -- \
  /etc/sudoers.d/strawberry-agent \
  /etc/systemd/system/strawberry-agent.service \
  /etc/systemd/system/strawberry-heartbeat.service \
  /etc/systemd/system/strawberry-heartbeat.timer \
  /etc/systemd/system/strawberry-state-backup.service \
  /etc/systemd/system/strawberry-state-backup.timer
systemctl daemon-reload
systemctl reset-failed

systemctl is-active --quiet home-agent.service
systemctl is-enabled --quiet home-agent-heartbeat.timer
if [[ -s /etc/home-agent/backup-repository && -d "$backup_checkout/.git" ]]; then
  systemctl is-enabled --quiet home-agent-state-backup.timer
fi
runuser -u "$new_agent" -- env \
  CODEX_HOME=/var/lib/home-agent/codex-home \
  /opt/home-agent/current/venv/bin/agentctl \
  --config /etc/home-agent/config.toml doctor --skip-telegram

echo "Migration complete: service identity and durable paths now use home-agent."
echo "Legacy filesystem and SSH command aliases remain for compatibility."
