#!/usr/bin/env bash
set -euo pipefail

agent_user="home-agent"
data_root="/var/lib/home-agent"
agentctl="/opt/home-agent/current/venv/bin/agentctl"
config="/etc/home-agent/config.toml"

if (($#)); then
  echo "Usage: home-agent-codex-bridge < prompt" >&2
  exit 2
fi
if [[ ! -x "$agentctl" ]]; then
  echo "Home Agent runtime is not installed." >&2
  exit 1
fi

exec sudo -n -u "$agent_user" env \
  HOME="$data_root" \
  CODEX_HOME="$data_root/codex-home" \
  "$agentctl" --config "$config" bridge
