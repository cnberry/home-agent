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

## Test design

- Treat `docs/BEHAVIOR.md` as the language-neutral behavior contract.
- Protect observable outcomes and failure boundaries, not Python implementation details.
- Prefer public CLI, transport, queue, and restored-data scenarios. Keep SDK-specific checks at the external adapter boundary.
- Do not assert source text, private helper calls, exact internal call counts, or incidental wording and model defaults.
- Keep the suite minimal by covering distinct risks and parameterizing input classes, not by maximizing test count.
- A rewrite in another language should retain the contract scenarios while replacing language-specific fixtures.
