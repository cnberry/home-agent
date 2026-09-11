# Behavior contract and testing

Home Agent is a single-owner, serial job runner. Telegram and the SSH bridge are
transports; Codex is the execution engine; SQLite is the current persistence
adapter. Keep these boundaries small rather than introducing another agent
framework. A replacement in another language should preserve the behaviors
below, not the Python classes, call sequences, or source layout.

## Acceptance contract

- Only the configured owner's private, original text messages may submit work.
  Unauthorized messages and duplicate deliveries must not execute commands.
- Accepted jobs persist across process exits, run one at a time in FIFO order,
  and respect input and queue limits. Telegram and heartbeat conversations stay
  separate; the SSH bridge shares the Telegram conversation.
- The optional panel worker has its own bounded FIFO, persisted conversation and
  restart recovery. A full Telegram queue does not consume panel capacity. Panel
  results/errors never use Telegram notifications. Workers share host resources.
- A transient failure before dispatch may retry. Once dispatch may have reached
  Codex, failure or restart must not automatically replay the job: side effects
  may already exist. An explicit owner retry is a new authorization.
- Stop and shutdown must prevent pending dispatch, interrupt active work, and
  leave the queue recoverable. Authentication failures retain queued work.
- New conversation requests must not race an active or newly claimed turn.
  Transport notification failures must not cause execution to replay.
- A healthy, idle heartbeat does not execute a turn. Due tasks, explicit
  instructions, changed alerts, and forced requests do. Equivalent alerts are
  throttled, but a failed alert job must not suppress future monitoring.
- Backup includes only the three durable Markdown files and a versioned manifest.
  Restore validates names, sizes, and hashes before replacing destination files.
  The bytes validated must be the bytes restored. A failed commit or push can
  be retried without changing durable content first.
- Invalid configuration produces an actionable failure rather than a traceback.
  Diagnostic output must redact supported credential forms.

## Test design

Use a few scenarios per boundary and parameterize meaningful input classes.
Assert accepted/rejected work, persisted status, outgoing messages, CLI exit
codes, and restored bytes. Avoid checking source strings, private methods,
implementation call counts, incidental wording, or a particular model default.

`tests/test_cli.py` invokes the installed executable with temporary configuration
and data. Set `HOME_AGENT_TEST_COMMAND` to an alternative command to reuse these
black-box checks against a replacement implementation. No Telegram credentials,
Codex login, or running worker are required for these cases.

Queue, worker, heartbeat, backup, and Telegram tests encode the same behavior
closer to their boundaries. Their Python fixtures will need replacement during
a language migration; their scenarios and observable outcomes should survive.
Use real temporary SQLite databases and Git repositories where practical. Fake
the external Codex/Telegram boundary, not the application's internal helpers.

SDK adapter tests intentionally remain Python-specific: they protect integration
with the pinned SDK, including the point where dispatch becomes uncertain.
ShellCheck, systemd unit validation, and distribution installation checks belong
in CI rather than Python tests that search shell scripts for particular strings.

Run `ruff check .`, `mypy src`, and `pytest` after Python changes. A green suite is
not a live-host deployment test. Restore replaces each file atomically, but does
not promise a transaction across all three files on power loss or disk failure.

## Interaction evidence and daily development

- Every authorized inbound message, queue transition/attempt, response and transport outcome
  has a durable timestamped private event; retrying never erases prior attempt history.
- At local midnight the installed cron schedule starts an independent improvement review.
  Reviews never consume the Telegram FIFO or conversation, never overlap, and do not silently
  replay an uncertain review. They preserve private evidence and compare real measurements.
- Improvements use synthetic evaluation, passing CI, normal PR merge rules and versioned
  releases. Deployment pauses new claims and drains active work while retaining queued tasks;
  failed deployment restores the previous code release. No supported change may weaken owner
  checks or uncertain-action no-replay guarantees.
