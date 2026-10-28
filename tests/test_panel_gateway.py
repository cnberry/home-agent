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
            self,
            prompt: str,
            *,
            thread_id: str | None,
            on_thread: Callable[[str], None],
            on_turn_started: Callable[[], None],
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
    first = await runtime.run(
        "first", thread_id=None, on_thread=lambda _: None, on_turn_started=lambda: None
    )
    assert connections[0].closed
    if fail_second:
        with pytest.raises(TimeoutError):
            await runtime.run(
                "next",
                thread_id=first.thread_id,
                on_thread=lambda _: None,
                on_turn_started=lambda: None,
            )
    else:
        await runtime.run(
            "next",
            thread_id=first.thread_id,
            on_thread=lambda _: None,
            on_turn_started=lambda: None,
        )
    assert len(connections) == 2
    assert connections[1].thread == first.thread_id
    assert connections[1].closed


@pytest.mark.asyncio
async def test_panel_socket_wakes_independent_queue_without_replaying(settings: Settings) -> None:
    import asyncio
    from dataclasses import replace

    from test_worker import Runtime

    from home_agent.control import ControlServer, submit
    from home_agent.database import Database
    from home_agent.panel_gateway import PanelNotifier
    from home_agent.worker import Worker

    main = Database(settings.database_path, max_queue=1)
    main.initialize()
    main.enqueue("telegram", "occupied")
    # A Telegram routing config must not enable a second Qwen device worker.
    panel = panel_settings(replace(settings, routing_config=settings.data_dir / "absent.json"))
    assert panel.routing_config is None
    db = Database(panel.database_path, max_queue=1)
    db.initialize()
    runtime = Runtime("Panel answer")
    worker = Worker(db, runtime, PanelNotifier(), poll_seconds=3600, event_driven=True)
    control = ControlServer(db, worker, panel.database_path.with_suffix(".sock"))
    worker.on_result = control.notify
    await control.start()
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.sleep(0.01)
        first = await asyncio.wait_for(
            asyncio.to_thread(
                submit, control.path, "panel request", 2, "bridge", "stable-panel-id"
            ),
            3,
        )
        repeated = await asyncio.to_thread(
            submit, control.path, "panel request", 2, "bridge", "stable-panel-id"
        )
        assert first == repeated
        assert first["response"] == "Panel answer"
        assert len(runtime.requests) == 1
        assert main.snapshot().queued == 1
        assert db.snapshot().queued == 0
        assert not settings.telegram_token_file.exists()
    finally:
        await worker.stop()
        await task
        await control.close()
