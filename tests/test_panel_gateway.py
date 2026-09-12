from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from home_agent.codex_runtime import CodexResult
from home_agent.config import Settings
from home_agent.panel_gateway import PanelRuntime, panel_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_second", [False, True])
async def test_panel_renews_connection_but_preserves_conversation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, fail_second: bool
) -> None:
    connections: list[Any] = []

    class Connection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert all(c.closed for c in connections)
            self.closed = False
            self.thread: str | None = None
            connections.append(self)

        async def run(
            self, prompt: str, *, thread_id: str | None,
            on_thread: Callable[[str], None], on_turn_started: Callable[[], None],
        ) -> CodexResult:
            self.thread = thread_id
            on_thread("panel-conversation")
            on_turn_started()
            if fail_second and thread_id is not None:
                raise TimeoutError("stalled backend")
            return CodexResult("panel-conversation", "reply")

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("home_agent.panel_gateway.CodexRuntime", Connection)
    runtime = PanelRuntime(panel_settings(settings))
    first = await runtime.run("first", thread_id=None, on_thread=lambda _: None,
                              on_turn_started=lambda: None)
    assert connections[0].closed
    if fail_second:
        with pytest.raises(TimeoutError):
            await runtime.run("next", thread_id=first.thread_id, on_thread=lambda _: None,
                              on_turn_started=lambda: None)
    else:
        await runtime.run("next", thread_id=first.thread_id, on_thread=lambda _: None,
                          on_turn_started=lambda: None)
    assert len(connections) == 2
    assert connections[1].thread == first.thread_id
    assert connections[1].closed
