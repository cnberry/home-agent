from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from home_agent import __version__
from home_agent.codex_runtime import (
    CodexInterrupted,
    CodexResult,
    CodexRunError,
)
from home_agent.database import Database, Job
from home_agent.performance import Performance
from home_agent.routing import LocalRouter

LOGGER = logging.getLogger(__name__)
REDACTIONS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk[-_]|gh[opsur]_|github_pat_)[A-Za-z0-9_-]{16,}\b"),
)


def safe_error(error: BaseException | str) -> str:
    value = str(error).replace("\n", " ").strip()
    for pattern in REDACTIONS:
        value = pattern.sub("[REDACTED]", value)
    if value:
        return value[:500]
    return error.__class__.__name__ if isinstance(error, BaseException) else "error"


class Notifier(Protocol):
    async def working(self, job: Job) -> None: ...

    async def completed(self, job: Job, response: str) -> None: ...

    async def failed(self, job: Job, message: str) -> None: ...

    async def retrying(self, job: Job, delay_seconds: int) -> None: ...


class AgentRuntime(Protocol):
    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None,
        on_thread: Callable[[str], None],
        on_turn_started: Callable[[], None],
    ) -> CodexResult: ...

    async def interrupt(self) -> bool: ...

    async def archive(self, thread_id: str) -> None: ...

    async def close(self) -> None: ...


class Worker:
    RETRY_DELAYS = (30, 120, 300)

    def __init__(
        self,
        database: Database,
        codex: AgentRuntime,
        notifier: Notifier,
        *,
        poll_seconds: float = 1.0,
        routing_config: Path | None = None,
    ) -> None:
        self.database = database
        self.codex = codex
        self.notifier = notifier
        self.poll_seconds = poll_seconds
        self.router = LocalRouter(routing_config) if routing_config else None
        self.on_result: Callable[[], None] = lambda: None
        self.performance: Performance | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._cancelled_job_id: int | None = None
        self._thread_lock = asyncio.Lock()

    def wake(self) -> None:
        """Wake an idle worker after a producer added work in this process."""
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            # Clear before claiming so a producer racing with claim_next either
            # leaves a queued job for this pass or leaves the wake event set.
            self._wake.clear()
            if (
                self.router is None
                and self.database.get_metadata("authentication_degraded") is True
            ):
                await self._wait_for_activity()
                continue
            async with self._thread_lock:
                if self._stop.is_set():
                    break
                job = self.database.claim_next()
                if job is not None:
                    await self._process(job)
            if job is None:
                await self._wait_for_activity()
                continue

    async def _wait_for_activity(self) -> None:
        if self._stop.is_set() or self._wake.is_set():
            return
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            await asyncio.wait(
                (stop_task, wake_task),
                timeout=self._next_wait(),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (stop_task, wake_task):
                task.cancel()
            await asyncio.gather(stop_task, wake_task, return_exceptions=True)

    def _next_wait(self) -> float | None:
        if self.router is None:
            return self.poll_seconds
        if self.database.get_metadata("deployment_paused"):
            return None
        with self.database.connect() as db:
            row = db.execute("SELECT MIN(available_at) FROM jobs WHERE status='queued'").fetchone()
        if row and row[0]:
            return max(
                0.0, (datetime.fromisoformat(row[0]) - datetime.now(timezone.utc)).total_seconds()
            )
        return None

    async def stop(self) -> None:
        self._stop.set()
        if self.router:
            await self.router.interrupt()
        await self.codex.interrupt()

    async def interrupt(self) -> bool:
        active = self.database.active_job()
        if active is None:
            return False
        self._cancelled_job_id = active.id
        if self.router:
            await self.router.interrupt()
        await self.codex.interrupt()
        return True

    async def archive_telegram_thread(self) -> bool:
        snapshot = self.database.snapshot()
        if snapshot.active is not None or snapshot.queued:
            return False
        async with self._thread_lock:
            snapshot = self.database.snapshot()
            if snapshot.active is not None or snapshot.queued:
                return False
            thread_id = self.database.get_thread("telegram")
            if thread_id:
                try:
                    await self.codex.archive(thread_id)
                except Exception as exc:
                    LOGGER.warning("could not archive Codex thread: %s", safe_error(exc))
            self.database.clear_thread("telegram")
        return True

    async def _notify(self, operation: Callable[[], Awaitable[None]]) -> None:
        try:
            await operation()
        except Exception as exc:
            LOGGER.warning("Telegram notification failed: %s", safe_error(exc))

    async def _process(self, job: Job) -> None:
        self.performance = Performance(self.database, job)
        try:
            await self._process_impl(job)
        finally:
            self.performance.finish()
            self.on_result()

    async def _process_impl(self, job: Job) -> None:
        LOGGER.info("job_started id=%s kind=%s attempt=%s", job.id, job.kind, job.attempts)
        await self._notify(lambda: self.notifier.working(job))
        self.database.record_event(
            "runtime_config",
            job_id=job.id,
            details={
                "version": __version__,
                "model": getattr(self.codex, "model", None),
                "reasoning_effort": getattr(self.codex, "reasoning_effort", None),
            },
        )
        if self.router and job.kind == "telegram":
            assert self.performance is not None

            def before_dispatch() -> None:
                if self._cancelled_job_id == job.id or self._stop.is_set():
                    raise RuntimeError("Cancelled before local dispatch")

            try:
                local = await self.router.run(job.prompt, self.performance, before_dispatch)
            except Exception as exc:
                # Adapter may have started: never fall back blindly after an unclassified exception.
                local = {
                    "outcome": "uncertain",
                    "reply": "Local request failed; no action was repeated.",
                }
                self.performance.event("local_error", error_type=type(exc).__name__)
            outcome = local["outcome"]
            self.performance.data["outcome"] = outcome
            if outcome != "fallback":
                if outcome == "clarify":
                    self.performance.data["eligible"] = False
                status: Literal["completed", "failed", "cancelled", "uncertain"] = (
                    "completed"
                    if outcome in ("confirmed", "observed", "clarify")
                    else "cancelled"
                    if outcome == "cancelled"
                    else "uncertain"
                    if outcome == "uncertain"
                    else "failed"
                )
                self.database.finish(
                    job.id,
                    status,
                    error=local["reply"] if status != "completed" else None,
                    response=local["reply"] if status == "completed" else None,
                )
                if job.telegram_chat_id is not None:
                    if status == "completed":
                        await self._notify(lambda: self.notifier.completed(job, local["reply"]))
                    else:
                        await self._notify(lambda: self.notifier.failed(job, local["reply"]))
                self._cancelled_job_id = None
                return
            self.performance.data.update(engine="codex", fallback_reason=local.get("reason"))
            self.performance.event("fallback", reason=local.get("reason"))
            if self.database.get_metadata("authentication_degraded") is True:
                self.database.finish(
                    job.id,
                    "failed",
                    "Codex authentication unavailable; local controls remain enabled.",
                )
                await self._notify(
                    lambda: self.notifier.failed(job, "Codex authentication unavailable.")
                )
                return
        thread_id = self.database.get_thread(job.kind)

        def check_interrupted() -> None:
            if self._cancelled_job_id == job.id:
                raise CodexInterrupted("cancelled before the turn began", turn_started=False)
            if self._stop.is_set():
                raise CodexInterrupted("Home Agent is stopping", turn_started=False)

        def mark_turn_started() -> None:
            check_interrupted()
            self.database.mark_codex_started(job.id)

        try:
            check_interrupted()
            result = await self.codex.run(
                job.prompt,
                thread_id=thread_id,
                on_thread=lambda value: self.database.set_thread(job.kind, value),
                on_turn_started=mark_turn_started,
            )
        except CodexInterrupted as exc:
            cancelled = self._cancelled_job_id == job.id
            self._cancelled_job_id = None
            current = self.database.get_job(job.id) or job
            if not cancelled and not exc.turn_started and not current.codex_started:
                self.database.requeue(job.id, 0, safe_error(exc))
                return
            interrupt_status: Literal["cancelled", "uncertain"] = (
                "cancelled" if cancelled else "uncertain"
            )
            message = "Cancelled by owner." if cancelled else safe_error(exc)
            self.database.finish(job.id, interrupt_status, message)
            await self._notify(lambda: self.notifier.failed(job, message))
            LOGGER.info("job_%s id=%s", interrupt_status, job.id)
            return
        except CodexRunError as exc:
            error = safe_error(exc)
            current = self.database.get_job(job.id) or job
            if self._cancelled_job_id == job.id:
                self._cancelled_job_id = None
                self.database.finish(job.id, "cancelled", "Cancelled by owner.")
                await self._notify(lambda: self.notifier.failed(job, "Cancelled by owner."))
            elif exc.authentication:
                self.database.set_metadata("authentication_degraded", True)
                self.database.finish(
                    job.id,
                    "uncertain" if exc.turn_started or current.codex_started else "failed",
                    error,
                )
                message = (
                    "Codex authentication needs attention. "
                    "Run `agentctl auth` on the Home Agent host."
                )
                await self._notify(lambda: self.notifier.failed(job, message))
            elif (
                exc.transient
                and not exc.turn_started
                and not current.codex_started
                and current.attempts <= len(self.RETRY_DELAYS)
            ):
                delay = self.RETRY_DELAYS[current.attempts - 1]
                self.database.requeue(job.id, delay, error)
                await self._notify(lambda: self.notifier.retrying(job, delay))
            else:
                run_error_status: Literal["uncertain", "failed"] = (
                    "uncertain" if exc.turn_started or current.codex_started else "failed"
                )
                self.database.finish(job.id, run_error_status, error)
                await self._notify(
                    lambda: self.notifier.failed(job, f"{run_error_status.title()}: {error}")
                )
            LOGGER.warning("job_error id=%s started=%s error=%s", job.id, exc.turn_started, error)
            return
        except Exception as exc:
            error = safe_error(exc)
            current = self.database.get_job(job.id) or job
            unexpected_status: Literal["cancelled", "uncertain", "failed"]
            if self._cancelled_job_id == job.id:
                unexpected_status = "cancelled"
                error = "Cancelled by owner."
                self._cancelled_job_id = None
            else:
                unexpected_status = "uncertain" if current.codex_started else "failed"
            self.database.finish(job.id, unexpected_status, error)
            await self._notify(
                lambda: self.notifier.failed(job, f"{unexpected_status.title()}: {error}")
            )
            LOGGER.error("unexpected job failure id=%s error=%s", job.id, error)
            return

        self._cancelled_job_id = None
        assert self.performance is not None
        self.performance.data["outcome"] = "codex_completed"
        self.database.finish(job.id, "completed", response=result.response)
        self.database.set_metadata("authentication_degraded", False)
        self.database.set_metadata(
            "last_successful_turn",
            {
                "job_id": job.id,
                "kind": job.kind,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        job = self.database.get_job(job.id) or job
        bridge_job = (
            job.kind == "telegram" and job.telegram_chat_id is None and job.ack_message_id is None
        )
        if not bridge_job and (job.kind != "heartbeat" or result.response.strip() != "NOOP"):
            await self._notify(lambda: self.notifier.completed(job, result.response))
        LOGGER.info("job_completed id=%s kind=%s", job.id, job.kind)
