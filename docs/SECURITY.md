# Security model

## Trust boundary

Codex deliberately has full access, no approval prompts, and unrestricted passwordless sudo.
The process starts under the dedicated `home-agent` Unix UID, but that UID is an auditing
and file-ownership convention rather than a security boundary: Codex can become root with
`sudo -n` whenever a task requires it. The main systemd service therefore does not apply settings
that would block setuid execution, capabilities, namespaces, or other normal root operations.

The agent can access the network and the entire host, including system files and its own ChatGPT
credentials. A malicious prompt, compromised dependency, or command can steal credentials,
destroy data, or take over the machine. Use a dedicated bot, keep the machine single purpose,
enable strong security on the owner Telegram account, and treat Telegram access as root access.

## Messaging boundary

The gateway requires all of the following before recording or acting on an update:

- exact configured numeric sender ID;
- exact private chat ID matching that sender;
- Telegram private-chat type;
- a non-bot sender;
- an original, unedited update.

No inbound webhook, Codex WebSocket, HTTP service, or MCP endpoint is exposed. Telegram long
polling needs outbound HTTPS only. Update IDs are unique in SQLite to prevent Bot API replay after
restart.

## Secrets

- Telegram token: root-owned file, exposed to the service through `LoadCredential`.
- ChatGPT authentication: agent-owned mode-0700 Codex home.
- Backup deploy key: backup-user mode-0700 home. The agent cannot read it as its initial UID, but
  its intentional sudo authority allows root access to it.
- GitHub source access: required only for the human/root deployment workflow.

Logs redact recognizable Telegram, OpenAI, and GitHub token forms. The repository ignore and leak
scanning rules cover auth files, tokens, databases, logs, SSH private keys, environment files,
Codex sessions, and backup artifacts.

HTTP transport loggers are forced to warning level because Telegram request URLs contain the bot
token. User-visible and persisted exception text is passed through credential redaction before it
is reported.

## Backup boundary

The backup process reads only three allowlisted durable Markdown files through a shared read-only
group. It rejects extra names, symlinks, non-regular files, oversize content, and manifest hash
mismatches. The backup key must be installed as a write-enabled deploy key for only the configured
private state repository. Never point state backup at the public source repository or use a
personal SSH key.

## Reporting a vulnerability

Do not open a public issue containing credentials or exploit details. Contact the repository
owner privately, revoke affected Telegram/ChatGPT/GitHub credentials, stop the service, and
preserve only sanitized diagnostic evidence.
