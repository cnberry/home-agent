"""Best-effort secret redaction for private interaction records, never an export allowlist."""

from __future__ import annotations

import re

PATTERNS = (
    re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk[-_]|gh[opsur]_|github_pat_)[A-Za-z0-9_-]{16,}\b"),
)


def redact(value: str) -> str:
    for pattern in PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value
