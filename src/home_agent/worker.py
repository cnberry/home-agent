from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Literal, Protocol

from home_agent.codex_runtime import (
    CodexInterrupted,
    CodexRunError,
    CodexRuntime,
)
from home_agent.database import Database, Job

LOGGER = logging.getLogger(__name__)
REDACTIONS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:sk|gh[opsu])_[A-Za-z0-9_-]{16,}\b"),
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


class Worker:
    RETRY_DELAYS = (30, 120, 300)

    def __init__(
        self,
        database: Database,
        codex: CodexRuntime,
        notifier: Notifier,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self.database = database
        self.codex = codex
        self.notifier = notifier
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self._cancelled_job_id: int | None = None

    async def run(self) -> None:
        while not self._stop.is_set():
            if self.database.get_metadata("authentication_degraded") is True:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                continue
            job = self.database.claim_next()
            if job is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                continue
            await self._process(job)

    async def stop(self) -> None:
        self._stop.set()
        await self.codex.interrupt()

    async def interrupt(self) -> bool:
        active = self.database.active_job()
        if active is None:
            return False
        self._cancelled_job_id = active.id
        await self.codex.interrupt()
        return True

    async def archive_telegram_thread(self) -> bool:
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
        LOGGER.info("job_started id=%s kind=%s attempt=%s", job.id, job.kind, job.attempts)
        await self._notify(lambda: self.notifier.working(job))
        thread_id = self.database.get_thread(job.kind)

        def mark_turn_started() -> None:
            self.database.mark_codex_started(job.id)
            if self._cancelled_job_id == job.id:
                raise CodexInterrupted("cancelled before the turn began", turn_started=True)

        try:
            result = await self.codex.run(
                job.prompt,
                thread_id=thread_id,
                on_thread=lambda value: self.database.set_thread(job.kind, value),
                on_turn_started=mark_turn_started,
            )
        except CodexInterrupted as exc:
            cancelled = self._cancelled_job_id == job.id
            self._cancelled_job_id = None
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
                self.database.finish(job.id, "failed", error)
                message = (
                    "Codex authentication needs attention. "
                    "Run `agentctl auth` on the Home Agent host."
                )
                await self._notify(lambda: self.notifier.failed(job, message))
            elif (
                exc.transient
                and not exc.turn_started
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
            unexpected_status: Literal["uncertain", "failed"] = (
                "uncertain" if current.codex_started else "failed"
            )
            self.database.finish(job.id, unexpected_status, error)
            await self._notify(
                lambda: self.notifier.failed(job, f"{unexpected_status.title()}: {error}")
            )
            LOGGER.error("unexpected job failure id=%s error=%s", job.id, error)
            return

        self._cancelled_job_id = None
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
        bridge_job = job.kind == "telegram" and job.telegram_chat_id is None
        if not bridge_job and (job.kind != "heartbeat" or result.response.strip() != "NOOP"):
            await self._notify(lambda: self.notifier.completed(job, result.response))
        LOGGER.info("job_completed id=%s kind=%s", job.id, job.kind)
