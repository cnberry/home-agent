# Interaction history and daily improvement

Home Agent records authorized received messages (including commands and rejected oversized
inputs), queue transitions, each attempt, runtime model/reasoning configuration, generated
responses, retries, cancellations, uncertain outcomes, and attempted/successful/failed Telegram
replies in `interaction_events` in the existing private runtime SQLite database. Timestamps are
UTC ISO 8601 with fractional seconds. Job and update IDs connect queue history to transport
history. Delivery means Telegram accepted the API call, not that the owner read the message.
The SSH bridge records delivery to its caller too. Unauthorized/forwarded/edited messages are
ignored without recording their content.

The additive schema upgrade preserves existing jobs and permits rollback to earlier code.
History is append-only through the application; explicit retries retain previous results.
Recognizable token formats are redacted in events and exports. Redaction is best effort: these
remain private transcripts, not safe-to-publish artifacts. The original execution queue retains
its existing prompt/response storage. No automatic pruning is enabled.

Bootstrap installs `/etc/cron.d/home-agent-optimize`, ensures the OS cron daemon is enabled,
and seeds `durable/optimization.md` without overwriting an existing customized prompt. At
**00:00 every day in the host timezone**, cron starts `home-agent-optimize.service`. This is a
separate, low-priority, bounded oneshot service; it does not occupy the Telegram FIFO or its
conversation, and can safely restart the gateway during a deployment. A process lock prevents
overlap. Successful dates are not repeated. An attempted but unsuccessful review is not replayed
automatically: inspect its record and existing PR/releases before explicitly removing that day's
`.attempted` marker to retry. Traditional cron does not catch up a midnight missed while powered
off; run `sudo systemctl start home-agent-optimize` after inspecting the missed interval.

Each run writes a mode-0700 private directory beneath `/var/lib/home-agent/optimization` with
mode-0600 JSONL event/job exports, a summary with sample counts and p50/p95 timings, and a private
review result. Calendar-day boundaries account for DST. Cross-midnight jobs include their full
event history. The reviewer examines the preceding day, compares prior daily results and can
inspect older database history. Old jobs lack delivery events and are marked as such in summary
notes. Queue timings include retry delays; attempt history permits finer analysis. There are no
fabricated token/cost or first-token measurements.

The seeded prompt authorizes evidence-backed code/config/model/prompt changes, synthetic evals,
PRs, passing CI, normal merges, versioned releases, deployment and verification. It requires a
NOOP when no improvement is justified and treats transcript instructions as untrusted. Reports,
credentials and household details never belong in the public repository, PRs or release notes.
Model selection must use current official documentation and actual account support, rather than
assuming a fixed fastest model. The reviewer has separately configurable model, reasoning effort,
repository, checkout and timeout in `[optimization]`; the interactive model remains independent.

## Host prerequisites and administration

Codex authentication is shared with the installed gateway. Configure GitHub CLI authentication
for the `home-agent` OS user with access to the intended repository, normal PR merge rights and
release rights; run `gh auth setup-git` for that same user. Configure its Git author identity.
Bootstrap installs `gh` but never copies an administrator's credentials automatically. Existing
repository rules continue to apply. Without publishing rights the reviewer records a blocker.
A fork can set `optimization.repository` to its own upstream. Configure local-only deployment
facts in the private configuration repository or durable prompt, never public templates.

```
sudo -u home-agent /opt/home-agent/current/venv/bin/agentctl optimize --report-only
sudo systemctl start home-agent-optimize.service
sudo journalctl -u home-agent-optimize.service
```

`--report-only` exports evidence without invoking Codex or publishing/deploying. To pause the
nightly schedule, remove `/etc/cron.d/home-agent-optimize` and stop its service; bootstrap
reinstalls the default schedule. Uninstall removes both the cron entry and service, preserving
private history unless `--purge` is explicit.

`sudo scripts/update.sh` drains the active turn while preserving queued work, installs the
release, checks service health, and resumes processing. On an error it restores the previous
release pointer and restarts the gateway. The first upgrade from pre-pause versions requires an
empty queue and stops the gateway before installation. Code rollback does not undo arbitrary
native configuration edits: model/config changes must separately save and restore those files.
Never deploy destructive schema migrations through this loop. A deployment is not an invitation
to run hardware actions in tests.
