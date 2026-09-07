#!/usr/bin/env bash
set -euo pipefail

agent_user="home-agent"
install_root="/opt/home-agent/current"
workspace="/srv/home-agent/workspace"
data_root="/var/lib/home-agent"
python="$install_root/venv/bin/python"

if (($#)); then
  prompt="$*"
else
  prompt="-"
fi

if [[ ! -x "$python" ]]; then
  echo "Home Agent Codex runtime is not installed." >&2
  exit 1
fi

codex_bin=$(
  "$python" -c 'from codex_cli_bin import bundled_codex_path; print(bundled_codex_path())'
)
if [[ ! -x "$codex_bin" ]]; then
  echo "Bundled Codex executable is unavailable." >&2
  exit 1
fi

exec sudo -n -u "$agent_user" env \
  HOME="$data_root" \
  CODEX_HOME="$data_root/codex-home" \
  "$codex_bin" exec \
  --dangerously-bypass-approvals-and-sandbox \
  --color never \
  -C "$workspace" \
  "$prompt"
