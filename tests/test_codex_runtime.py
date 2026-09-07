from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import ReasoningEffort, TurnStatus

import home_agent.codex_runtime as runtime_module
from home_agent.codex_runtime import CodexRuntime


class FakeHandle:
    def __init__(self) -> None:
        self.interrupted = False

    async def run(self) -> Any:
        return SimpleNamespace(
            status=TurnStatus.completed,
            final_response="finished",
            error=None,
        )

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeThread:
    def __init__(self, thread_id: str, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.id = thread_id
        self.calls = calls

    async def turn(self, prompt: str, **kwargs: Any) -> FakeHandle:
        self.calls.append((f"turn:{prompt}", kwargs))
        return FakeHandle()


class FakeCodex:
    instance: FakeCodex

    def __init__(self, config: Any) -> None:
        self.config = config
        self.calls: list[tuple[str, dict[str, Any]]] = []
        FakeCodex.instance = self

    async def account(self, *, refresh_token: bool) -> Any:
        assert refresh_token
        return SimpleNamespace(account=object(), requires_openai_auth=True)

    async def thread_resume(self, thread_id: str, **kwargs: Any) -> FakeThread:
        self.calls.append((f"resume:{thread_id}", kwargs))
        return FakeThread(thread_id, self.calls)

    async def thread_start(self, **kwargs: Any) -> FakeThread:
        self.calls.append(("start", kwargs))
        return FakeThread("new-thread", self.calls)

    async def thread_archive(self, thread_id: str) -> None:
        self.calls.append((f"archive:{thread_id}", {}))

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_full_access_and_deny_all_are_reapplied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_module, "AsyncCodex", FakeCodex)
    runtime = CodexRuntime(
        tmp_path,
        tmp_path / "codex-home",
        timeout_seconds=10,
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )
    started: list[str] = []
    result = await runtime.run(
        "do work",
        thread_id="existing-thread",
        on_thread=started.append,
        on_turn_started=lambda: started.append("turn-started"),
    )

    assert result.response == "finished"
    assert started == ["existing-thread", "turn-started"]
    calls = FakeCodex.instance.calls
    for name, options in calls:
        if name.startswith(("resume:", "turn:")):
            assert options["sandbox"] == Sandbox.full_access
            assert options["approval_mode"] == ApprovalMode.deny_all
            assert options["cwd"] == str(tmp_path)
        if name.startswith("turn:"):
            assert options["model"] == "gpt-5.6-luna"
            assert options["effort"] == ReasoningEffort.low


@pytest.mark.asyncio
async def test_new_thread_also_uses_full_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_module, "AsyncCodex", FakeCodex)
    runtime = CodexRuntime(
        tmp_path,
        tmp_path / "codex-home",
        timeout_seconds=10,
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )
    await runtime.run(
        "new work",
        thread_id=None,
        on_thread=lambda _: None,
        on_turn_started=lambda: None,
    )
    name, options = FakeCodex.instance.calls[0]
    assert name == "start"
    assert options["sandbox"] == Sandbox.full_access
    assert options["approval_mode"] == ApprovalMode.deny_all
