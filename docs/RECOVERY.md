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
5. Enter the Telegram token locally and complete device-code authentication. See the
   [Telegram setup guide](../README.md#telegram-bot-setup) for bot creation and owner ID discovery.
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

Complete the Telegram checklist below after these host checks.

Run any machine-specific acceptance checks defined by the private deployment repository. Do not
send live device actions as part of an unattended migration check.

## Telegram end-to-end checklist

Status: **pending the first live deployment walkthrough**. Automated tests and CI do not count
as completion. Run these checks together on the dedicated agent host, using harmless prompts
only. Record the release commit, host, date, and pass/fail or not-run result for each check in
the private deployment repository; do not include tokens or raw credential-bearing logs.

1. Confirm that only the new host is polling the bot and that all setup helpers have exited.
   From the intended owner's private chat, send `/start` and `/status`. Expect help and healthy
   authentication status. A transport reply alone does not prove Codex can execute a turn.
2. Send `Reply exactly HOME-AGENT-OK. Do not use tools or modify files.` Expect acknowledgement
   and a completed Codex reply containing `HOME-AGENT-OK`.
3. Send `Run sleep 30, then reply WAIT-DONE. Do not modify any files.` While it is working,
   send `Reply SECOND-OK without using tools.` Confirm it queues behind the first job and
   `/status` never reports more than one active job. Let both finish in order.
4. Repeat the harmless waiting task and queue another harmless reply. While work is outstanding,
   `/new` should refuse. Send `/stop`; the active job should terminate without automatically
   replaying, while the queued reply remains eligible to run. Stop does not clear the queue.
5. Once the queue is empty, send `/new`, then another harmless reply request. Expect a new
   Telegram conversation and a successful reply.
6. Send `/heartbeat` and check `/status` for progress and completion. A successful `NOOP`
   intentionally has no completion notification; silence alone is not evidence of failure.
   Ensure durable heartbeat instructions contain no device-changing actions before this check.
7. From another Telegram account, send harmless text and `/status`. Expect no response and no
   additional queued job when the owner checks `/status`.
8. From the owner account, forward harmless text from another chat; expect it to be ignored.
   Edit an already completed harmless request; expect no second job for the edit.
9. Confirm BotFather refuses adding the bot to groups when group joining is disabled. To test
   the application's group rejection too, temporarily allow joins, add it to a controlled test
   group, and send `/status` addressed to the bot. Expect no reply or queued job, then remove
   the bot and disable group joining again. Record this check as not run if no test group is used.
10. With no active work, run `sudo systemctl restart home-agent.service`. Confirm `/status` and
    another harmless Codex request work without re-entering credentials. Keep crash/active-job
    recovery as a separate supervised fault-injection check, not an ordinary restart assumption.

If a check fails, stop before sending real host-control tasks. Inspect service status and the
local journal, but review and redact any diagnostic output before sharing it. Do not add sleep
delays or weaken automated assertions simply to make a failed live check look successful.

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
