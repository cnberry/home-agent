---
name: home-agent-codex
description: Send messages to the persistent Home Agent Codex agent through its FIFO Telegram conversation queue over SSH and return its replies in the current Codex task. Use whenever the user invokes $home-agent-codex, prefixes a message with @homeAgentCodex, or asks to message, consult, delegate to, or chat with the Home Agent.
---

# Home Agent Codex

Forward the user's message to Home Agent and present its response as the next conversational reply.

## Workflow

1. Treat text following `$home-agent-codex` or `@homeAgentCodex` as the message. Also accept the legacy `$strawberry-codex` and `@strawberryCodex` prefixes during migration. Preserve its wording and line breaks. If the user asks to forward surrounding context, include only the context they identify.
2. Run `scripts/send.py -- <message>`. For long or multiline content, pipe it to `scripts/send.py` on stdin instead of constructing fragile shell quoting.
3. Wait for the command to finish. The remote command submits to Home Agent's FIFO queue and shares its persisted Telegram conversation.
4. Return the bundled client's stdout as the answer. It prefixes every successful reply with `[HomeAgentCodex]` for clear conversation attribution. Do not remove the prefix, paraphrase the reply, or merge it with a separate local answer unless the user requests commentary.
5. If the command fails, report the concise stderr reason and keep the user's original message available for retry. Never fall back to running the task locally without saying so.

## Boundaries

- Read the SSH destination from `HOME_AGENT_SSH_HOST`, or from
  `${HOME_AGENT_SSH_HOST_FILE:-~/.config/home-agent/ssh-host}`. If neither is configured, stop
  and ask the user to install their private deployment settings.
- Do not print SSH diagnostics, credentials, Telegram tokens, or raw service logs as part of a successful reply.
- Do not bypass the queue with the one-shot `home-agent-codex` command; use `home-agent-codex-bridge` through the bundled script.
- Each invocation is one queued turn. Subsequent invocations continue the same Home Agent conversation.
- Home Agent has passwordless root authority. Forwarding a request authorizes it to act on that request with its configured privileges.
