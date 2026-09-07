from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / ".agents/skills/home-agent-codex/scripts/send.py"


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run the shipped CLI against an SSH process double, without a network."""
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['SSH_CAPTURE']).write_text(json.dumps(\n"
        "    {'arguments': sys.argv[1:], 'message': sys.stdin.read()}))\n"
        "sys.stdout.write(os.environ.get('SSH_REPLY', 'remote reply\\n'))\n"
        "sys.stderr.write(os.environ.get('SSH_ERROR', ''))\n"
        "sys.exit(int(os.environ.get('SSH_EXIT', '0')))\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SSH_CAPTURE", str(tmp_path / "request.json"))
    monkeypatch.delenv("HOME_AGENT_SSH_HOST", raising=False)
    monkeypatch.setenv("HOME_AGENT_SSH_HOST_FILE", str(tmp_path / "ssh-host"))

    def invoke(*args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLIENT), *args],
            input=stdin,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )

    return invoke


@pytest.mark.parametrize("source", ["environment", "file"])
def test_client_preserves_prompt_and_returns_attributed_reply(
    client: Callable[..., subprocess.CompletedProcess[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    if source == "environment":
        monkeypatch.setenv("HOME_AGENT_SSH_HOST", "operator@agent.example")
    else:
        (tmp_path / "ssh-host").write_text("operator@agent.example\n", encoding="utf-8")
    prompt = "  preserve indentation\n$(do-not-execute) `or-this` \u2603\n\n"

    result = client(stdin=prompt)

    assert result.returncode == 0
    assert result.stdout == "[HomeAgentCodex] remote reply\n"
    assert result.stderr == ""
    request = json.loads((tmp_path / "request.json").read_text())
    assert request["message"] == prompt
    assert request["arguments"][-2:] == ["operator@agent.example", "home-agent-codex-bridge"]


@pytest.mark.parametrize("source", ["environment", "file"])
def test_invalid_destination_never_starts_ssh(
    client: Callable[..., subprocess.CompletedProcess[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    if source == "environment":
        monkeypatch.setenv("HOME_AGENT_SSH_HOST", "-oProxyCommand=unexpected")
    else:
        (tmp_path / "ssh-host").write_text("-oProxyCommand=unexpected", encoding="utf-8")

    result = client("--", "do work")

    assert result.returncode == 2
    assert "invalid" in result.stderr
    assert not (tmp_path / "request.json").exists()


def test_missing_destination_never_starts_ssh(
    client: Callable[..., subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    result = client("--", "do work")

    assert result.returncode == 2
    assert "not configured" in result.stderr
    assert not (tmp_path / "request.json").exists()


@pytest.mark.parametrize("prefix", ["@homeAgentCodex", "$home-agent-codex"])
def test_invocation_prefix_is_removed_but_similar_text_is_preserved(
    client: Callable[..., subprocess.CompletedProcess[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prefix: str,
) -> None:
    monkeypatch.setenv("HOME_AGENT_SSH_HOST", "agent.example")
    prompt = f"{prefix}Examples should remain intact"

    result = client("--", f"{prefix}: {prompt}")

    assert result.returncode == 0
    request = json.loads((tmp_path / "request.json").read_text())
    assert request["message"] == prompt
    result = client("--", prompt)
    assert result.returncode == 0
    request = json.loads((tmp_path / "request.json").read_text())
    assert request["message"] == prompt


def test_remote_failure_preserves_exit_status_without_a_success_reply(
    client: Callable[..., subprocess.CompletedProcess[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME_AGENT_SSH_HOST", "agent.example")
    monkeypatch.setenv("SSH_EXIT", "23")
    monkeypatch.setenv("SSH_ERROR", "remote task failed\n")

    result = client("--", "do work")

    assert result.returncode == 23
    assert result.stdout == ""
    assert result.stderr == "remote task failed\n"
