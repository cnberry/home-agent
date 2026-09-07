from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from home_agent.codex_runtime import CodexResult, CodexRunError
from home_agent.database import Database, Job
from home_agent.worker import Worker, safe_error


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, str]] = []

    async def working(self, job: Job) -> None:
        self.events.append(("working", job.id, ""))

    async def completed(self, job: Job, response: str) -> None:
        self.events.append(("completed", job.id, response))

    async def failed(self, job: Job, message: str) -> None:
        self.events.append(("failed", job.id, message))

    async def retrying(self, job: Job, delay_seconds: int) -> None:
        self.events.append(("retrying", job.id, str(delay_seconds)))


class FakeCodex:
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.interrupted = False

    async def run(self, prompt: str, **kwargs: Any) -> CodexResult:
        kwargs["on_thread"]("thread-1")
        if self.outcome == "transient":
            raise CodexRunError(
                "connection timed out",
                turn_started=False,
                transient=True,
            )
        kwargs["on_turn_started"]()
        if self.outcome == "uncertain":
            raise CodexRunError("connection lost after start", turn_started=True)
        return CodexResult("thread-1", self.outcome)

    async def interrupt(self) -> bool:
        self.interrupted = True
        return True

    async def close(self) -> None:
        return None

    async def archive(self, thread_id: str) -> None:
        return None


def claimed_job(path: Path, kind: str = "telegram") -> tuple[Database, Job]:
    database = Database(path)
    database.initialize()
    chat_id = 123 if kind == "telegram" else None
    queued = database.enqueue(kind, "work", telegram_chat_id=chat_id)  # type: ignore[arg-type]
    assert queued is not None
    claimed = database.claim_next()
    assert claimed is not None
    return database, claimed


@pytest.mark.asyncio
async def test_success_and_noop_suppression(tmp_path: Path) -> None:
    database, job = claimed_job(tmp_path / "success.sqlite3")
    notifier = FakeNotifier()
    await Worker(database, FakeCodex("done"), notifier)._process(job)  # type: ignore[arg-type]
    assert database.get_job(job.id).status == "completed"  # type: ignore[union-attr]
    assert ("completed", job.id, "done") in notifier.events

    database, heartbeat = claimed_job(tmp_path / "heartbeat.sqlite3", "heartbeat")
    heartbeat_notifier = FakeNotifier()
    await Worker(database, FakeCodex("NOOP"), heartbeat_notifier)._process(heartbeat)  # type: ignore[arg-type]
    assert not any(event[0] == "completed" for event in heartbeat_notifier.events)


@pytest.mark.asyncio
async def test_bridge_job_stores_response_without_telegram_notification(tmp_path: Path) -> None:
    database = Database(tmp_path / "bridge.sqlite3")
    database.initialize()
    queued = database.enqueue("telegram", "work")
    assert queued is not None
    job = database.claim_next()
    assert job is not None
    notifier = FakeNotifier()
    await Worker(database, FakeCodex("bridge reply"), notifier)._process(job)  # type: ignore[arg-type]
    stored = database.get_job(job.id)
    assert stored is not None and stored.response == "bridge reply"
    assert not any(event[0] == "completed" for event in notifier.events)


@pytest.mark.asyncio
async def test_transient_pre_turn_failure_requeues(tmp_path: Path) -> None:
    database, job = claimed_job(tmp_path / "runtime.sqlite3")
    notifier = FakeNotifier()
    await Worker(database, FakeCodex("transient"), notifier)._process(job)  # type: ignore[arg-type]
    stored = database.get_job(job.id)
    assert stored is not None and stored.status == "queued"
    assert ("retrying", job.id, "30") in notifier.events


@pytest.mark.asyncio
async def test_post_start_failure_is_uncertain(tmp_path: Path) -> None:
    database, job = claimed_job(tmp_path / "runtime.sqlite3")
    notifier = FakeNotifier()
    await Worker(database, FakeCodex("uncertain"), notifier)._process(job)  # type: ignore[arg-type]
    stored = database.get_job(job.id)
    assert stored is not None and stored.status == "uncertain"
    assert database.retry(job.id).status == "queued"  # type: ignore[union-attr]


def test_secret_redaction() -> None:
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    assert token not in safe_error(f"request failed with {token}")
    assert "[REDACTED]" in safe_error(f"request failed with {token}")


class PreTurnCodex(FakeCodex):
    def __init__(self) -> None:
        super().__init__("unused")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, prompt: str, **kwargs: Any) -> CodexResult:
        kwargs["on_thread"]("thread-1")
        self.entered.set()
        await self.release.wait()
        kwargs["on_turn_started"]()
        return CodexResult("thread-1", "should not complete")

    async def interrupt(self) -> bool:
        self.release.set()
        return False


@pytest.mark.asyncio
async def test_stop_cancels_during_pre_turn_setup(tmp_path: Path) -> None:
    database, job = claimed_job(tmp_path / "runtime.sqlite3")
    notifier = FakeNotifier()
    codex = PreTurnCodex()
    worker = Worker(database, codex, notifier)  # type: ignore[arg-type]
    processing = asyncio.create_task(worker._process(job))
    await codex.entered.wait()
    assert await worker.interrupt()
    await processing
    stored = database.get_job(job.id)
    assert stored is not None and stored.status == "cancelled"
