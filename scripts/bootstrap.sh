#!/usr/bin/env bash
set -euo pipefail

agent_user="home-agent"
backup_user="home-agent-backup"
state_group="home-agent-state"
install_root="/opt/home-agent"
project_root="/srv/home-agent"
data_root="/var/lib/home-agent"
backup_root="/var/lib/home-agent-backup"
config_root="/etc/home-agent"
backup_repository_file="$config_root/backup-repository"
owner_id=""
token_source=""
non_interactive=false
skip_auth=false

usage() {
  echo "Usage: sudo $0 [--owner-id ID] [--token-file PATH] [--non-interactive] [--skip-auth]"
}

while (($#)); do
  case "$1" in
    --owner-id) owner_id="${2:?missing owner ID}"; shift 2 ;;
    --token-file) token_source="${2:?missing token path}"; shift 2 ;;
    --non-interactive) non_interactive=true; shift ;;
    --skip-auth) skip_auth=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "bootstrap must run as root" >&2
  exit 1
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_root=$(cd -- "$script_dir/.." && pwd)

if [[ ! -r /etc/os-release ]]; then
  echo "Cannot identify Linux distribution" >&2
  exit 1
fi
# shellcheck source=/dev/null
. /etc/os-release
distro_family=""
distro_ids=" ${ID:-} ${ID_LIKE:-} "
case "$distro_ids" in
  *" debian "*|*" ubuntu "*) distro_family="debian" ;;
  *" arch "*|*" omarchy "*) distro_family="arch" ;;
  *)
    echo "Supported targets are Ubuntu, Debian, Arch Linux, and Omarchy; found ${ID:-unknown}" >&2
    exit 1
    ;;
esac
if [[ ! -d /run/systemd/system ]]; then
  echo "systemd is required" >&2
  exit 1
fi
case "$(uname -m)" in
  x86_64|aarch64|arm64) ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

if [[ "$distro_family" == "debian" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates curl git openssh-client python3 python3-venv sudo cron gh
else
  pacman -S --needed --noconfirm \
    ca-certificates curl git openssh python python-pip sudo cronie github-cli
fi

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
PY

getent group "$state_group" >/dev/null || groupadd --system "$state_group"
nologin_shell=$(command -v nologin || true)
if [[ -z "$nologin_shell" ]]; then
  echo "Cannot find the nologin account shell" >&2
  exit 1
fi
if ! id "$agent_user" >/dev/null 2>&1; then
  useradd --system --home-dir "$data_root" --shell "$nologin_shell" --user-group "$agent_user"
fi
if ! id "$backup_user" >/dev/null 2>&1; then
  useradd --system --home-dir "$backup_root" --shell "$nologin_shell" --user-group "$backup_user"
fi
usermod --home "$data_root" --shell "$nologin_shell" --append -G "$state_group" "$agent_user"
usermod --home "$backup_root" --shell "$nologin_shell" --append -G "$state_group" "$backup_user"

install -d -o root -g root -m 0755 "$install_root/releases" "$project_root"
install -d -o "$agent_user" -g "$agent_user" -m 0700 "$data_root/runtime" "$data_root/codex-home"
install -d -o "$agent_user" -g "$state_group" -m 2750 "$data_root/durable"
install -d -o "$agent_user" -g "$agent_user" -m 0750 "$project_root/workspace"
install -d -o "$agent_user" -g "$agent_user" -m 0700 "$project_root/development" "$data_root/optimization"
install -d -o "$backup_user" -g "$backup_user" -m 0700 "$backup_root" "$backup_root/.ssh"
install -d -o root -g "$state_group" -m 0750 "$config_root"
install -d -o root -g root -m 0700 "$config_root/credentials"

if git -C "$source_root" rev-parse --verify HEAD >/dev/null 2>&1; then
  version=$(git -C "$source_root" rev-parse --short=12 HEAD)
else
  version="source-$(date -u +%Y%m%d%H%M%S)"
fi
release_dir="$install_root/releases/$version"
if [[ ! -f "$release_dir/.build-complete" ]]; then
  install -d -o root -g root -m 0755 "$release_dir"
  if git -C "$source_root" rev-parse --verify HEAD >/dev/null 2>&1; then
    git -C "$source_root" archive HEAD | tar -x -C "$release_dir"
  else
    cp -a "$source_root/." "$release_dir/"
    rm -rf -- "$release_dir/.git"
  fi
  chown -R root:root "$release_dir"
  chmod -R go-w "$release_dir"
  python3 -m venv "$release_dir/venv"
  "$release_dir/venv/bin/python" -m pip install --disable-pip-version-check --require-hashes -r "$release_dir/requirements.lock"
  "$release_dir/venv/bin/python" -m pip install --disable-pip-version-check \
    --no-build-isolation --no-deps "$release_dir"
  "$release_dir/venv/bin/python" -c 'import home_agent'
  touch "$release_dir/.build-complete"
fi
install -o root -g root -m 0755 "$source_root/scripts/home-agent-codex.sh" \
  /usr/local/bin/home-agent-codex
install -o root -g root -m 0755 "$source_root/scripts/home-agent-codex-bridge.sh" \
  /usr/local/bin/home-agent-codex-bridge
sudoers_file="/etc/sudoers.d/home-agent"
install -o root -g root -m 0440 "$source_root/deploy/sudoers/home-agent" "$sudoers_file"
visudo -cf "$sudoers_file"

if [[ ! -d "$project_root/.git" ]]; then
  git -C "$project_root" init -q
fi
install -o root -g root -m 0644 "$source_root/deploy/AGENTS.runtime.md" "$project_root/AGENTS.md"

for state_file in tasks.md memory.md heartbeat.md optimization.md; do
  if [[ ! -e "$data_root/durable/$state_file" ]]; then
    install -o "$agent_user" -g "$state_group" -m 0640 "$source_root/deploy/durable/$state_file" "$data_root/durable/$state_file"
  fi
done

config_file="$config_root/config.toml"
if [[ ! -e "$config_file" ]]; then
  if [[ -z "$owner_id" && "$non_interactive" == false ]]; then
    read -r -p "Telegram numeric owner ID: " owner_id
  fi
  if [[ ! "$owner_id" =~ ^[1-9][0-9]*$ ]]; then
    echo "A positive --owner-id is required for first installation" >&2
    exit 1
  fi
  install -o root -g "$state_group" -m 0640 "$source_root/deploy/config.example.toml" "$config_file"
  sed -i "s/^owner_id = .*/owner_id = $owner_id/" "$config_file"
fi

token_file="$config_root/credentials/telegram-token"
if [[ -n "$token_source" ]]; then
  install -o root -g root -m 0600 "$token_source" "$token_file"
elif [[ ! -e "$token_file" ]]; then
  if [[ "$non_interactive" == true ]]; then
    echo "--token-file is required for a non-interactive first installation" >&2
    exit 1
  fi
  read -r -s -p "Telegram bot token: " telegram_token
  echo
  if [[ "$telegram_token" != *:* ]]; then
    echo "Telegram token is malformed" >&2
    exit 1
  fi
  umask 077
  printf '%s\n' "$telegram_token" > "$token_file"
  unset telegram_token
fi

for unit in "$source_root"/deploy/systemd/*; do
  install -o root -g root -m 0644 "$unit" "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload

HOME_AGENT_CONFIG="$config_file" TELEGRAM_TOKEN_PATH="$token_file" \
  "$release_dir/venv/bin/python" - <<'PY'
import asyncio
import os
from pathlib import Path
from telegram import Bot

async def check() -> None:
    token = Path(os.environ["TELEGRAM_TOKEN_PATH"]).read_text().strip()
    identity = await Bot(token).get_me()
    print(f"Telegram bot validated: @{identity.username}")

asyncio.run(check())
PY

runuser -u "$agent_user" -- env \
  CODEX_HOME="$data_root/codex-home" \
  "$release_dir/venv/bin/agentctl" --config "$config_file" init-db

if [[ "$skip_auth" == false && "$non_interactive" == false ]]; then
  runuser -u "$agent_user" -- env \
    CODEX_HOME="$data_root/codex-home" \
    "$release_dir/venv/bin/agentctl" --config "$config_file" auth
fi

runuser -u "$agent_user" -- \
  "$release_dir/venv/bin/agentctl" --config "$config_file" doctor --skip-telegram

ln -sfn "$release_dir" "$install_root/current.new"
mv -Tf "$install_root/current.new" "$install_root/current"

if [[ -L "$data_root/.ssh" ]]; then
  echo "$data_root/.ssh must not be a symlink" >&2
  exit 1
fi
rm -f -- "$data_root/.ssh/authorized_keys"
backup_configured=false
if [[ -s "$backup_repository_file" || -n ${HOME_AGENT_BACKUP_REPOSITORY:-} ]]; then
  backup_configured=true
  "$source_root/scripts/configure-backup.sh"
else
  echo "Durable-state backup skipped: install a private repository URL at $backup_repository_file."
fi
if [[ "$non_interactive" == false && "$backup_configured" == true ]]; then
  read -r -p "Backup action after adding the deploy key ([i]nitialize/[r]estore/[s]kip): " backup_action
  case "$backup_action" in
    i|I) HOME_AGENT_DEFER_ENABLE=1 "$source_root/scripts/configure-backup.sh" --initialize ;;
    r|R) HOME_AGENT_DEFER_ENABLE=1 "$source_root/scripts/configure-backup.sh" --restore ;;
    *) echo "Backup initialization skipped; run scripts/configure-backup.sh later." ;;
  esac
fi

systemctl enable home-agent.service home-agent-heartbeat.timer
systemctl restart home-agent.service
systemctl start home-agent-heartbeat.timer
if [[ "$backup_configured" == true && -d "$backup_root/repo/.git" ]]; then
  systemctl enable --now home-agent-state-backup.timer
else
  systemctl disable --now home-agent-state-backup.timer 2>/dev/null || true
fi
install -d -o root -g root -m 0755 /etc/cron.d
install -o root -g root -m 0644 "$source_root/deploy/cron.d/home-agent-optimize" /etc/cron.d/home-agent-optimize
if [[ "$distro_family" == "debian" ]]; then
  systemctl enable --now cron.service
else
  systemctl enable --now cronie.service
fi
echo "Nightly improvement review installed for midnight in the host timezone."
echo "Home Agent installed at release $version."
echo "Configure a private state repository, then run scripts/configure-backup.sh."
