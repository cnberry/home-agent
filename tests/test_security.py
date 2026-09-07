from __future__ import annotations

import logging
from pathlib import Path

from home_agent.cli import configure_logging

ROOT = Path(__file__).resolve().parents[1]


def test_main_service_starts_as_agent_and_allows_root_escalation() -> None:
    unit = (ROOT / "deploy/systemd/home-agent.service").read_text(encoding="utf-8")
    assert "User=home-agent" in unit
    assert "Group=home-agent" in unit
    assert "NoNewPrivileges=" not in unit
    assert "CapabilityBoundingSet=" not in unit
    assert "RestrictAddressFamilies=" not in unit
    assert "RestrictSUIDSGID=" not in unit
    assert "ProtectSystem=" not in unit
    assert "LoadCredential=telegram-token:" in unit


def test_repository_excludes_secret_and_runtime_artifacts() -> None:
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("auth.json", "telegram-token", "*.sqlite3", "*.log", "id_ed25519*"):
        assert pattern in ignored


def test_runtime_identity_and_durable_rules_are_immutable_source() -> None:
    instructions = (ROOT / "deploy/AGENTS.runtime.md").read_text(encoding="utf-8")
    assert "dedicated Home Agent Linux computer" in instructions
    assert "may use `sudo -n` to run as root" in instructions
    assert "/var/lib/home-agent/durable/tasks.md" in instructions


def test_credential_bearing_transport_loggers_never_emit_info() -> None:
    configure_logging(verbose=True)
    for logger_name in ("httpx", "httpcore", "telegram.ext.ExtBot"):
        assert logging.getLogger(logger_name).level == logging.WARNING
