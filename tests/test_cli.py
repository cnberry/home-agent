"""Executable contracts: set HOME_AGENT_TEST_COMMAND to test another implementation."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture
def cli_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    data = tmp_path / "data"
    paths = {
        "workspace": tmp_path / "workspace",
        "data_dir": data,
        "database_path": data / "runtime" / "runtime.sqlite3",
        "durable_dir": data / "durable",
        "codex_home": data / "codex-home",
    }
    for key in ("workspace", "durable_dir", "codex_home"):
        paths[key].mkdir(parents=True)
    path.write_text(
        "[telegram]\nowner_id = 123456789\n[agent]\n"
        + "\n".join(f"{key} = {json.dumps(str(value))}" for key, value in paths.items())
        + "\nmax_input_chars = 64\nmax_queue = 1\n"
        + f"[backup]\ncheckout = {json.dumps(str(tmp_path / 'backup'))}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def cli(cli_config: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    configured = os.environ.get("HOME_AGENT_TEST_COMMAND")
    command = shlex.split(configured) if configured else [
        str(Path(sys.executable).with_name("agentctl"))
    ]

    def run(*args: str, input: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, "--config", str(cli_config), *args],
            input=input,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )

    return run


def test_offline_installation_is_idempotent(
    cli: Callable[..., subprocess.CompletedProcess[str]], tmp_path: Path
) -> None:
    for _ in range(2):
        result = cli("doctor", "--offline", "--json")
        assert result.returncode == 0, result.stderr or result.stdout
        checks = json.loads(result.stdout)
        assert checks and all(check["ok"] for check in checks)
        assert cli("init-db").returncode == 0
    (tmp_path / "workspace").rmdir()
    result = cli("doctor", "--offline", "--json")
    assert result.returncode == 1
    assert any(not check["ok"] for check in json.loads(result.stdout))


@pytest.mark.parametrize("content", ["[invalid", "[telegram]\nowner_id = true\n"])
def test_invalid_configuration_exits_cleanly(
    cli: Callable[..., subprocess.CompletedProcess[str]], cli_config: Path, content: str
) -> None:
    cli_config.write_text(content, encoding="utf-8")
    result = cli("doctor", "--offline")
    assert result.returncode == 2
    assert result.stderr.strip()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("prompt", [" \n", "x" * 65])
def test_rejected_bridge_input_does_not_consume_capacity(
    cli: Callable[..., subprocess.CompletedProcess[str]], prompt: str
) -> None:
    rejected = cli("bridge", "--wait-timeout", "0", input=prompt)
    assert rejected.returncode == 2
    assert rejected.stderr.strip()
    accepted = cli("bridge", "--wait-timeout", "0", input="valid prompt")
    assert accepted.returncode == 124, accepted.stderr


def test_bridge_timeout_keeps_work_durable_and_queue_bounded(
    cli: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    first = cli("bridge", "--wait-timeout", "0", input="first task")
    assert first.returncode == 124, first.stderr
    assert first.stderr.strip()
    second = cli("bridge", "--wait-timeout", "0", input="second task")
    assert second.returncode == 1, second.stderr
    assert second.stderr.strip()
