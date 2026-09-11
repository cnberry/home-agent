"""Dedicated panel FIFO and conversation using the shared Home Agent runtime."""
from __future__ import annotations

import asyncio
import fcntl
import signal
from dataclasses import replace

from home_agent.codex_runtime import CodexRuntime
from home_agent.config import Settings
from home_agent.database import Database, Job
from home_agent.worker import Worker

PANEL_INSTRUCTIONS = """You are the persistent Home Agent on the owner's home server.
The private panel gateway has authenticated the owner. Treat their spoken text as their task.
Follow the repository AGENTS.md instruction chain and keep secrets out of responses and logs.
You run without Codex sandboxing or approval prompts as the home-agent Linux user;
use sudo -n only when the owner's task requires it. Verify work before reporting success.
This conversation is dedicated to a small touch panel, separate from Telegram.
Respond concisely in plain text, with the result first. Avoid Markdown tables and long preambles.
Other Home Agent conversations can operate concurrently; inspect current state before changing it.
"""


def panel_settings(settings: Settings) -> Settings:
    path = settings.database_path
    return replace(settings, database_path=path.with_name(path.stem + ".panel" + path.suffix))


class PanelNotifier:
    """Results are read by the panel bridge; no outbound messaging transport."""

    async def working(self, job: Job) -> None:
        pass

    async def completed(self, job: Job, response: str) -> None:
        pass

    async def failed(self, job: Job, message: str) -> None:
        pass

    async def retrying(self, job: Job, delay_seconds: int) -> None:
        pass


async def run_panel(settings: Settings) -> None:
    settings = panel_settings(settings)
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = settings.database_path.with_suffix(".worker.lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        database = Database(settings.database_path, settings.max_queue)
        database.initialize()
        database.recover_interrupted()
        runtime = CodexRuntime(
            settings.workspace,
            settings.codex_home,
            timeout_seconds=settings.turn_timeout_seconds,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
            developer_instructions=PANEL_INSTRUCTIONS,
            client_title="Home Agent Panel",
        )
        worker = Worker(
            database, runtime, PanelNotifier(), poll_seconds=settings.worker_poll_seconds
        )
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopped.set)
        try:
            database.set_metadata("authentication_degraded", not await runtime.authenticated())
            running = asyncio.create_task(worker.run())
            stopping = asyncio.create_task(stopped.wait())
            try:
                done, _ = await asyncio.wait(
                    (running, stopping), return_when=asyncio.FIRST_COMPLETED
                )
                if running in done:
                    await running
                else:
                    await worker.stop()
                    await asyncio.wait_for(running, 30)
            finally:
                stopping.cancel()
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, stopping, return_exceptions=True)
        finally:
            await runtime.close()
            for signum in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(signum)
