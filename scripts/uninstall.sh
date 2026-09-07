#!/usr/bin/env bash
set -euo pipefail

purge=false
if [[ ${1:-} == "--purge" ]]; then
  purge=true
elif (($#)); then
  echo "Usage: sudo $0 [--purge]" >&2
  exit 2
fi
if [[ ${EUID} -ne 0 ]]; then
  echo "uninstall must run as root" >&2
  exit 1
fi

systemctl disable --now home-agent.service home-agent-heartbeat.timer home-agent-state-backup.timer 2>/dev/null || true
rm -f -- \
  /etc/sudoers.d/home-agent \
  /etc/systemd/system/home-agent.service \
  /etc/systemd/system/home-agent-heartbeat.service \
  /etc/systemd/system/home-agent-heartbeat.timer \
  /etc/systemd/system/home-agent-state-backup.service \
  /etc/systemd/system/home-agent-state-backup.timer
systemctl daemon-reload
rm -rf -- /opt/home-agent
rm -f -- \
  /usr/local/bin/home-agent-codex \
  /usr/local/bin/home-agent-codex-bridge \
  /usr/local/bin/strawberry-codex \
  /usr/local/bin/strawberry-codex-bridge

if [[ "$purge" == true ]]; then
  rm -rf -- /srv/home-agent /var/lib/home-agent /var/lib/home-agent-backup /etc/home-agent
  userdel home-agent 2>/dev/null || true
  userdel home-agent-backup 2>/dev/null || true
  groupdel home-agent-state 2>/dev/null || true
  for legacy_path in \
    /opt/strawberry-agent \
    /srv/strawberry-agent \
    /var/lib/strawberry-agent \
    /var/lib/strawberry-agent-backup \
    /etc/strawberry-agent; do
    if [[ -L "$legacy_path" ]]; then
      rm -f -- "$legacy_path"
    fi
  done
  echo "Installation, credentials, and durable state were permanently removed."
else
  echo "Software removed. Configuration, credentials, and durable state were preserved."
fi
