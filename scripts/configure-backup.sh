#!/usr/bin/env bash
set -euo pipefail

backup_user="home-agent-backup"
backup_root="/var/lib/home-agent-backup"
repository="${HOME_AGENT_BACKUP_REPOSITORY:-}"
repository_file="${HOME_AGENT_BACKUP_REPOSITORY_FILE:-/etc/home-agent/backup-repository}"
key="$backup_root/.ssh/id_ed25519"
action="show"

as_backup() {
  runuser -u "$backup_user" -- env -i \
    HOME="$backup_root" \
    PATH=/usr/bin:/bin \
    GIT_SSH_COMMAND="/usr/bin/ssh -i $key -o IdentitiesOnly=yes" \
    "$@"
}

while (($#)); do
  case "$1" in
    --initialize) action="initialize"; shift ;;
    --restore) action="restore"; shift ;;
    --repository) repository="${2:?missing repository}"; shift 2 ;;
    -h|--help)
      echo "Usage: sudo $0 [--repository URL] [--initialize|--restore]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ ${EUID} -ne 0 ]]; then
  echo "configure-backup must run as root" >&2
  exit 1
fi
if ! id "$backup_user" >/dev/null 2>&1; then
  echo "Run bootstrap.sh first" >&2
  exit 1
fi
if [[ -z "$repository" && -r "$repository_file" ]]; then
  IFS= read -r repository < "$repository_file"
fi
if [[ -z "$repository" ]]; then
  echo "Backup repository is not configured." >&2
  echo "Set HOME_AGENT_BACKUP_REPOSITORY or install one URL in $repository_file." >&2
  exit 1
fi
if [[ "$repository" == -* || "$repository" =~ [[:space:]] ]]; then
  echo "Backup repository must be a single Git URL without whitespace." >&2
  exit 1
fi

install -d -o "$backup_user" -g "$backup_user" -m 0700 "$backup_root/.ssh"
if [[ ! -e "$key" ]]; then
  as_backup ssh-keygen -q -t ed25519 -N '' -C 'home-agent-state-backup' -f "$key"
fi
if [[ -L "$key" || ! -f "$key" || -L "$key.pub" || ! -f "$key.pub" ]]; then
  echo "Backup SSH key paths must be regular files" >&2
  exit 1
fi
chown "$backup_user:$backup_user" "$key" "$key.pub"
chmod 0600 "$key"
chmod 0644 "$key.pub"
known_hosts_tmp=$(mktemp)
trap 'rm -f -- "$known_hosts_tmp"' EXIT
# Published by GitHub at https://docs.github.com/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
printf '%s\n' 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' > "$known_hosts_tmp"
install -o "$backup_user" -g "$backup_user" -m 0600 "$known_hosts_tmp" "$backup_root/.ssh/known_hosts"

echo "Add this as a write-enabled deploy key on the configured private backup repository:"
cat "$key.pub"

if [[ "$action" == "show" ]]; then
  echo "After adding the deploy key, rerun with --initialize or --restore."
  exit 0
fi

checkout="$backup_root/repo"
if [[ -L "$checkout" ]]; then
  echo "$checkout must not be a symlink" >&2
  exit 1
fi
if [[ ! -d "$checkout/.git" ]]; then
  as_backup git clone "$repository" "$checkout"
else
  as_backup git -C "$checkout" remote set-url origin "$repository"
fi
as_backup git -C "$checkout" config user.name "home-agent-state-backup"
as_backup git -C "$checkout" config user.email "home-agent-state-backup@users.noreply.github.com"
as_backup git -C "$checkout" config core.sshCommand "/usr/bin/ssh -i $key -o IdentitiesOnly=yes"
remote_state=false
if as_backup git -C "$checkout" ls-remote --exit-code --heads origin state-backup >/dev/null 2>&1; then
  remote_state=true
  as_backup git -C "$checkout" fetch origin state-backup
  as_backup git -C "$checkout" switch -C state-backup origin/state-backup
else
  as_backup git -C "$checkout" switch --orphan state-backup
  as_backup git -C "$checkout" rm -rf --ignore-unmatch .
fi
if [[ "$action" == "restore" ]]; then
  if [[ "$remote_state" == false ]]; then
    echo "The remote state-backup branch does not exist." >&2
    exit 1
  fi
  HOME_AGENT_CONFIG=/etc/home-agent/config.toml \
    /opt/home-agent/current/venv/bin/agentctl restore
  chown home-agent:home-agent-state /var/lib/home-agent/durable/*.md
  chmod 0640 /var/lib/home-agent/durable/*.md
else
  as_backup env HOME_AGENT_CONFIG=/etc/home-agent/config.toml \
    /opt/home-agent/current/venv/bin/agentctl backup
fi
if [[ ${HOME_AGENT_DEFER_ENABLE:-0} != 1 ]]; then
  systemctl enable --now home-agent-state-backup.timer
fi
echo "Durable-state backup $action completed."
