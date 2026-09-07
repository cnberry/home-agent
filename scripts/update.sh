#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "update must run as root" >&2
  exit 1
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
"$script_dir/bootstrap.sh" --non-interactive --skip-auth
systemctl restart home-agent.service
systemctl --no-pager --full status home-agent.service
