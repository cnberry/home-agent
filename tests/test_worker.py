from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from home_agent.codex_runtime import CodexInterrupted, CodexResult, CodexRunError
from home_agent.database import Database, Job
from home_agent.worker import Worker


class Notifications:
    def __init__(self, *, failure: str | None = None) -> None:
        self.events: list[tuple[str, int, str]] = []
        self.failure = failure
        self.working_started = asyncio.Event()
        self.working_release: asyncio.Event | None = None

    async def working(self, job: Job) -> None:
        self.working_started.set()
        if self.working_release is not None:
            await self.working_release.wait()
        if self.failure:
            raise RuntimeError(self.failure)

    async def completed(self, job: Job, response: str) -> None:
        self.events.append(("completed", job.id, response))

    async def failed(self, job: Job, message: str) -> None:
        self.events.append(("failed", job.id, message))

    async def retrying(self, job: Job, delay_seconds: int) -> None:
        self.events.append(("retrying", job.id, str(delay_seconds)))


class Runtime:
    def __init__(
        self,
        outcome: str | Exception = "done",
        *,
        before_turn: bool = False,
        pause: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.before_turn = before_turn
        self.pause = pause
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = False
        self.requests: list[tuple[str, str | None]] = []
        self.archived: list[str] = []

    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None,
        on_thread: Callable[[str], None],
        on_turn_started: Callable[[], None],
    ) -> CodexResult:
        self.requests.append((prompt, thread_id))
        thread_id = thread_id or f"thread-{len(self.requests)}"
        on_thread(thread_id)
        if self.pause == "setup":
            self.entered.set()
            await self.release.wait()
        if not self.before_turn:
            on_turn_started()
        if self.pause == "turn":
            self.entered.set()
            await self.release.wait()
            if self.interrupted:
                raise CodexInterrupted("interrupted", turn_started=True)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return CodexResult(thread_id, self.outcome)

    async def interrupt(self) -> bool:
        self.interrupted = True
        self.release.set()
        return self.pause == "turn"

    async def archive(self, thread_id: str) -> None:
        self.archived.append(thread_id)

    async def close(self) -> None:
        pass


def queue(path: Path) -> Database:
    database = Database(path / "runtime.sqlite3")
    database.initialize()
    return database


async def wait_for_status(database: Database, job_id: int, status: str) -> Job:
    async def wait() -> Job:
        while True:
            job = database.get_job(job_id)
            if job is not None and job.attempts and job.status == status:
                return job
            await asyncio.sleep(0.001)

    return await asyncio.wait_for(wait(), 2)


async def run_job(
    worker: Worker, database: Database, job_id: int, status: str = "completed"
) -> Job:
    task = asyncio.create_task(worker.run())
    try:
        return await wait_for_status(database, job_id, status)
    finally:
        await worker.stop()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("authentication_degraded", [False, True])
async def test_idle_worker_accepts_new_work(
    tmp_path: Path, authentication_degraded: bool
) -> None:
    database = queue(tmp_path)
    database.set_metadata("authentication_degraded", authentication_degraded)
    notifications = Notifications()
    worker = Worker(database, Runtime(), notifications, poll_seconds=0.001)
    task = asyncio.create_task(worker.run())
    try:
        # Let the polling wait expire before new work becomes available.
        await asyncio.sleep(0.01)
        database.set_metadata("authentication_degraded", False)
        job = database.enqueue("telegram", "new work", telegram_chat_id=123)
        assert job is not None
        stored = await wait_for_status(database, job.id, "completed")
        assert stored.response == "done"
        assert notifications.events == [("completed", job.id, "done")]
    finally:
        await worker.stop()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "chat_id", "response", "notify"),
    [
        ("telegram", 123, "done", True),
        ("telegram", None, "bridge reply", False),
        ("heartbeat", None, "NOOP", False),
        ("heartbeat", None, "disk nearly full", True),
    ],
)
async def test_completed_jobs_store_results_and_notify_their_recipient(
    tmp_path: Path, kind: str, chat_id: int | None, response: str, notify: bool
) -> None:
    database = queue(tmp_path)
    job = database.enqueue(kind, "work", telegram_chat_id=chat_id)
    assert job is not None
    notifications = Notifications()
    worker = Worker(database, Runtime(response), notifications, poll_seconds=0.001)
    result = await run_job(worker, database, job.id)
    assert result.response == response
    assert notifications.events == ([("completed", job.id, response)] if notify else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_turn", "reported_started", "expected"),
    [(True, False, "queued"), (False, True, "uncertain"), (False, False, "uncertain")],
)
async def test_only_failures_before_dispatch_are_retried_automatically(
    tmp_path: Path, before_turn: bool, reported_started: bool, expected: str
) -> None:
    database = queue(tmp_path)
    job = database.enqueue("telegram", "change something", telegram_chat_id=123)
    assert job is not None
    runtime = Runtime(
        CodexRunError("connection lost", turn_started=reported_started, transient=True),
        before_turn=before_turn,
    )
    notifications = Notifications()
    worker = Worker(database, runtime, notifications, poll_seconds=0.001)
    stored = await run_job(worker, database, job.id, expected)
    assert stored.attempts == 1
    assert len(runtime.requests) == 1
    assert notifications.events[0][0] == ("retrying" if expected == "queued" else "failed")


@pytest.mark.asyncio
async def test_authentication_failure_pauses_remaining_work(tmp_path: Path) -> None:
    database = queue(tmp_path)
    first = database.enqueue("telegram", "first")
    second = database.enqueue("telegram", "second")
    assert first is not None and second is not None
    runtime = Runtime(
        CodexRunError("expired login", turn_started=False, authentication=True),
        before_turn=True,
    )
    notifications = Notifications()
    worker = Worker(database, runtime, notifications, poll_seconds=0.001)
    await run_job(worker, database, first.id, "failed")
    assert database.get_metadata("authentication_degraded") is True
    remaining = database.get_job(second.id)
    assert remaining is not None and remaining.status == "queued" and remaining.attempts == 0
    assert "authentication" in notifications.events[0][2].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["notification", "setup", "turn"])
async def test_owner_cancellation_does_not_start_or_replay_work(
    tmp_path: Path, stage: str
) -> None:
    database = queue(tmp_path)
    job = database.enqueue("telegram", "work", telegram_chat_id=123)
    assert job is not None
    notifications = Notifications()
    runtime = Runtime(pause=stage)
    if stage == "notification":
        notifications.working_release = asyncio.Event()
    worker = Worker(database, runtime, notifications, poll_seconds=0.001)
    task = asyncio.create_task(worker.run())
    try:
        entered = notifications.working_started if stage == "notification" else runtime.entered
        await asyncio.wait_for(entered.wait(), 2)
        assert await worker.interrupt()
        if notifications.working_release:
            notifications.working_release.set()
        stored = await wait_for_status(database, job.id, "cancelled")
        assert bool(stored.codex_started) is (stage == "turn")
        if stage == "notification":
            assert not runtime.requests
    finally:
        await worker.stop()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_shutdown_before_dispatch_leaves_work_for_restart(tmp_path: Path) -> None:
    database = queue(tmp_path)
    job = database.enqueue("telegram", "work")
    assert job is not None
    runtime = Runtime(pause="setup")
    worker = Worker(database, runtime, Notifications(), poll_seconds=0.001)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(runtime.entered.wait(), 2)
    await worker.stop()
    await asyncio.wait_for(task, 2)
    stored = database.get_job(job.id)
    assert stored is not None and stored.status == "queued" and not stored.codex_started


@pytest.mark.asyncio
async def test_conversation_history_is_separate_and_new_clears_only_telegram(
    tmp_path: Path,
) -> None:
    database = queue(tmp_path)
    runtime = Runtime()
    notifications = Notifications()
    worker = Worker(database, runtime, notifications, poll_seconds=0.001)
    task = asyncio.create_task(worker.run())
    try:
        for kind, prompt in [("telegram", "first"), ("heartbeat", "check"), ("telegram", "next")]:
            job = database.enqueue(kind, prompt)
            assert job is not None
            await wait_for_status(database, job.id, "completed")
        assert runtime.requests == [("first", None), ("check", None), ("next", "thread-1")]
        assert await worker.archive_telegram_thread()
        job = database.enqueue("telegram", "fresh")
        assert job is not None
        await wait_for_status(database, job.id, "completed")
        assert runtime.requests[-1] == ("fresh", None)
        assert database.get_thread("heartbeat") == "thread-2"
    finally:
        await worker.stop()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_notification_failure_does_not_fail_job(tmp_path: Path) -> None:
    database = queue(tmp_path)
    job = database.enqueue("telegram", "work", telegram_chat_id=123)
    assert job is not None
    notifications = Notifications(failure="Telegram is unavailable")
    worker = Worker(database, Runtime(), notifications, poll_seconds=0.001)
    await run_job(worker, database, job.id)
    assert notifications.events == [("completed", job.id, "done")]
