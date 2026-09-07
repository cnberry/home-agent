# Clean-system recovery and Omarchy migration

A rebuild intentionally requires a new Telegram token entry, local ChatGPT device login, and a
new backup deploy key. GitHub contains no secret recovery material.

## One-time rename on the existing host

After checking out the first `home-agent` release on the current host, preserve the existing
Telegram queue, Codex authentication and sessions, durable state, token, and backup checkout with:

```bash
sudo ./scripts/migrate-from-strawberry-agent.sh
```

The migration stops the legacy services, renames their Unix accounts and persistent directories,
installs the new units, switches the backup remote, and validates the new service. Compatibility
links keep legacy paths and SSH commands working. It does not change the OS host name; perform that
separately when the replacement name is known.

## Before rebuilding

If the old host still works, force one final curated-state backup and confirm it reached the
configured private state repository:

```bash
sudo systemctl start home-agent-state-backup.service
sudo journalctl -u home-agent-state-backup.service -n 50 --no-pager
```

Save the owner ID and the reviewed release tag or commit. Do not copy SQLite, Codex sessions,
ChatGPT authentication, the Telegram token, logs, or either SSH private key.

Confirm the private deployment repository is synchronized too. It should own the backup target,
SSH destination, machine configuration, schedules, and other private operational details; none of
that deployment data belongs in this repository.

## Rebuild

1. Install a supported Ubuntu/Debian or Arch/Omarchy release with systemd and apply OS updates.
2. Clone the Home Agent source repository.
3. Check out the reviewed release tag or commit.
4. Run `sudo ./scripts/bootstrap.sh`.
5. Enter the Telegram token locally and complete device-code authentication.
6. Restore the private deployment repository and install its backup repository URL at
   `/etc/home-agent/backup-repository`.
7. Run `sudo ./scripts/configure-backup.sh`, add the newly printed public key as a write-enabled
   deploy key scoped only to the private state repository, and rerun with `--restore`.
8. The restore validates the manifest, hashes, file set, symlink rules, and size limits before
   replacing durable Markdown state. Confirm the service, timers, owner DM response, and forced
   heartbeat.

For an Omarchy rebuild, finish the Omarchy first-run setup before bootstrapping the agent. Set the
intended new host name, select the intended local timezone, confirm that `sudo`, SSH, and GitHub
access work, and then run the steps above as the regular administrator account. The bootstrap uses
`pacman` on Arch-family systems and does not run a full system upgrade.

If backup initialization was skipped during bootstrap:

```bash
sudo ./scripts/configure-backup.sh
# Add the displayed public key on GitHub.
sudo ./scripts/configure-backup.sh --restore
sudo systemctl restart home-agent.service
```

## Acceptance checks

```bash
sudo systemctl is-active home-agent.service
sudo systemctl is-enabled home-agent-heartbeat.timer home-agent-state-backup.timer
sudo -u home-agent id
sudo -u home-agent sudo -n true  # must succeed
sudo -u home-agent sudo -n id -u  # must print 0
sudo visudo -cf /etc/sudoers.d/home-agent
```

From Telegram, verify `/status`, send two rapid harmless messages, interrupt a harmless long turn
with `/stop`, and force `/heartbeat`. Confirm another account and a group produce no response.

Run any machine-specific acceptance checks defined by the private deployment repository. Do not
send live device actions as part of an unattended migration check.

## Restore service coordination

Both restore entry points pause the agent, heartbeat, and state-backup activity
before changing the checkout or durable files. Successful backup-setup restores
resume previously active units; `scripts/restore.sh` also starts the agent, as it
did before. An inactive timer is not enabled merely because a restore succeeded.

If restore fails after services are stopped, they remain stopped and the script
prints a recovery warning. Resolve the reported failure and rerun the restore
before restarting work. Manifest validation precedes destination writes, but
replacement is atomic per file, not across all three durable files: a disk error
or power loss can still interrupt a multi-file restore.

The prior active-unit list is held only for the current restore run. After a
failed run, explicitly restart the agent and any previously used timers once
recovery succeeds; a rerun cannot infer which stopped timers were previously
active. Do not run simultaneous restores or manually start these services while
a restore is in progress.
