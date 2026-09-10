#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "update must run as root" >&2
  exit 1
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec 9>/run/lock/home-agent-deploy.lock
flock -n 9 || { echo "Another deployment is running" >&2; exit 1; }
previous=$(readlink -f /opt/home-agent/current)
agentctl="$previous/venv/bin/agentctl"
config=/etc/home-agent/config.toml
paused=false

resume() {
  if [[ "$paused" == true ]]; then
    runuser -u home-agent -- "$agentctl" --config "$config" deployment-pause --resume
  fi
}
rollback() {
  trap - ERR
  echo "Deployment failed; restoring $previous" >&2
  ln -sfn "$previous" /opt/home-agent/current.rollback
  mv -Tf /opt/home-agent/current.rollback /opt/home-agent/current
  resume || true
  systemctl restart home-agent.service
  exit 1
}
trap resume EXIT
# The first upgrade from a release without pause support must have an idle queue.
if "$agentctl" --help | grep -q deployment-pause; then
  runuser -u home-agent -- "$agentctl" --config "$config" deployment-pause
  paused=true
else
  runuser -u home-agent -- "$previous/venv/bin/python" - "$config" <<'PY'
import sys
from pathlib import Path
from home_agent.config import load_settings
from home_agent.database import Database
s = load_settings(Path(sys.argv[1]))
db = Database(s.database_path)
if db.active_job() or db.snapshot().queued:
    raise SystemExit("Drain existing work before the first upgrade to deployment-pause support")
PY
  systemctl stop home-agent.service
fi
trap rollback ERR
"$script_dir/bootstrap.sh" --non-interactive --skip-auth
systemctl restart home-agent.service
runuser -u home-agent -- /opt/home-agent/current/venv/bin/agentctl --config "$config" doctor --skip-telegram
systemctl is-active --quiet home-agent.service
resume
paused=false
systemctl --no-pager status home-agent.service
