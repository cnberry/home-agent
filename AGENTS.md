# Home Agent repository

## Project context

- Home Agent runs on a separate Linux computer, currently Ubuntu and planned for Omarchy.
- Do not treat the current Mac running Codex as the Home Agent host.
- Unless the user says otherwise, provide Linux-specific commands and guidance for that host.

## Engineering expectations

- Keep the agent runtime based solely on the stable OpenAI Codex Python SDK.
- Keep Telegram as a transport layer; do not add another agent framework.
- Run `ruff check .`, `mypy src`, and `pytest` after Python changes.
- Never commit Telegram tokens, Codex credentials, deploy keys, runtime databases, logs, or transcripts.
- Home Agent Codex should have full access as a user would, escalating to sudo when needed.
- Keep bootstrap and recovery procedures idempotent and suitable for clean Ubuntu/Debian and
  Arch/Omarchy systems.
