from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / ".agents/skills/home-agent-codex/scripts/send.py"


def load_client() -> ModuleType:
    spec = importlib.util.spec_from_file_location("home_agent_skill_client", CLIENT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ssh_destination_comes_from_private_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host_file = tmp_path / "ssh-host"
    host_file.write_text("operator@agent.example\n", encoding="utf-8")
    monkeypatch.delenv("HOME_AGENT_SSH_HOST", raising=False)
    monkeypatch.setenv("HOME_AGENT_SSH_HOST_FILE", str(host_file))

    assert load_client().ssh_host() == "operator@agent.example"


def test_missing_ssh_destination_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HOME_AGENT_SSH_HOST", raising=False)
    monkeypatch.setenv("HOME_AGENT_SSH_HOST_FILE", str(tmp_path / "missing"))

    with pytest.raises(ValueError, match="not configured"):
        load_client().ssh_host()
