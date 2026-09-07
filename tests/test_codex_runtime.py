from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.errors import ServerBusyError
from openai_codex.types import ReasoningEffort, TurnStatus

import home_agent.codex_runtime as runtime_module
from home_agent.codex_runtime import CodexInterrupted, CodexRunError, CodexRuntime


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace only the external SDK; exercise Home Agent's public runtime API."""
    handle = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                status=TurnStatus.completed, final_response=" finished \n", error=None
            )
        ),
        interrupt=AsyncMock(),
    )
    thread = SimpleNamespace(id="thread-1", turn=AsyncMock(return_value=handle))
    client = SimpleNamespace(
        account=AsyncMock(
            return_value=SimpleNamespace(account=object(), requires_openai_auth=True)
        ),
        thread_start=AsyncMock(return_value=thread),
        thread_resume=AsyncMock(return_value=thread),
    )
    monkeypatch.setattr(runtime_module, "AsyncCodex", lambda config: client)
    return SimpleNamespace(client=client, thread=thread, handle=handle)


@pytest.fixture
def runtime(sdk: SimpleNamespace, tmp_path: Path) -> CodexRuntime:
    return CodexRuntime(
        tmp_path,
        tmp_path / "codex-home",
        timeout_seconds=10,
        model="gpt-5.6-luna",
        reasoning_effort="low",
    )


async def run(runtime: CodexRuntime, thread_id: str | None = None) -> Any:
    return await runtime.run(
        "do work", thread_id=thread_id, on_thread=lambda _: None, on_turn_started=lambda: None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", [None, "thread-1"])
async def test_conversation_runs_with_configured_access_and_model(
    runtime: CodexRuntime, sdk: SimpleNamespace, thread_id: str | None
) -> None:
    result = await run(runtime, thread_id)

    assert (result.thread_id, result.response) == ("thread-1", "finished")
    setup = sdk.client.thread_start if thread_id is None else sdk.client.thread_resume
    for call in (setup.await_args, sdk.thread.turn.await_args):
        assert call.kwargs["sandbox"] == Sandbox.full_access
        assert call.kwargs["approval_mode"] == ApprovalMode.deny_all
        assert call.kwargs["cwd"] == str(runtime.workspace)
        assert call.kwargs["model"] == "gpt-5.6-luna"
    assert sdk.thread.turn.await_args.kwargs["effort"] == ReasoningEffort.low


@pytest.mark.asyncio
async def test_unauthenticated_request_does_not_dispatch(
    runtime: CodexRuntime, sdk: SimpleNamespace
) -> None:
    sdk.client.account.return_value.account = None

    with pytest.raises(CodexRunError) as raised:
        await run(runtime)

    assert raised.value.authentication and not raised.value.turn_started
    sdk.thread.turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (ConnectionError("connection lost"), True),
        (ServerBusyError(-32001, "overloaded"), True),
        (ValueError("missing conversation"), False),
    ],
)
async def test_resume_failure_preserves_conversation(
    runtime: CodexRuntime, sdk: SimpleNamespace, error: Exception, transient: bool
) -> None:
    sdk.client.thread_resume.side_effect = error

    with pytest.raises(CodexRunError) as raised:
        await run(runtime, "thread-1")

    assert not raised.value.turn_started
    assert raised.value.transient is transient
    sdk.client.thread_start.assert_not_awaited()
    sdk.thread.turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_dispatch_reply_is_fenced_before_remote_work(
    runtime: CodexRuntime, sdk: SimpleNamespace
) -> None:
    persisted: list[str] = []

    async def dispatch(*args: Any, **kwargs: Any) -> None:
        assert persisted == ["thread-1", "dispatched"]
        raise ConnectionError("reply lost after server accepted turn")

    sdk.thread.turn.side_effect = dispatch
    with pytest.raises(CodexRunError) as raised:
        await runtime.run(
            "do work",
            thread_id=None,
            on_thread=persisted.append,
            on_turn_started=lambda: persisted.append("dispatched"),
        )

    assert raised.value.turn_started and not raised.value.transient


@pytest.mark.asyncio
async def test_cancelled_dispatch_callback_prevents_remote_work(
    runtime: CodexRuntime, sdk: SimpleNamespace
) -> None:
    def cancelled() -> None:
        raise CodexInterrupted("cancelled", turn_started=False)

    with pytest.raises(CodexInterrupted) as raised:
        await runtime.run(
            "do work", thread_id=None, on_thread=lambda _: None, on_turn_started=cancelled
        )

    assert not raised.value.turn_started
    sdk.thread.turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_during_dispatch_reaches_the_accepted_turn(
    runtime: CodexRuntime, sdk: SimpleNamespace
) -> None:
    dispatching, accepted = asyncio.Event(), asyncio.Event()

    async def dispatch(*args: Any, **kwargs: Any) -> Any:
        dispatching.set()
        await accepted.wait()
        return sdk.handle

    sdk.thread.turn.side_effect = dispatch
    sdk.handle.run.return_value.status = TurnStatus.interrupted
    task = asyncio.create_task(run(runtime))
    await asyncio.wait_for(dispatching.wait(), timeout=2)
    assert await runtime.interrupt()
    accepted.set()
    with pytest.raises(CodexInterrupted):
        await asyncio.wait_for(task, timeout=2)

    sdk.handle.interrupt.assert_awaited_once()
    assert not await runtime.interrupt()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["account", "run"])
async def test_deadline_covers_setup_and_turn_and_survives_interrupt_failure(
    runtime: CodexRuntime, sdk: SimpleNamespace, phase: str
) -> None:
    finished = asyncio.Event()

    async def blocked(*args: Any, **kwargs: Any) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    operation = sdk.client.account if phase == "account" else sdk.handle.run
    operation.side_effect = blocked
    sdk.handle.interrupt.side_effect = ConnectionError("transport closed")
    runtime.timeout_seconds = 1

    with pytest.raises(CodexInterrupted) as raised:
        await run(runtime)

    assert raised.value.turn_started is (phase == "run")
    assert finished.is_set()
    assert not await runtime.interrupt()
