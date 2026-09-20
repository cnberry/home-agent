"""Delivery runs independently of device execution; retries never rerun jobs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from home_agent.database import Database, Job
from home_agent.performance import BOOT_ID


class Outbox:
    def __init__(self, database: Database, send: Callable[[Job, str], Awaitable[Any]]):
        self.database, self.send = database, send
        self.wake = asyncio.Event()
        self.stopped = False

    def notify(self) -> None:
        self.wake.set()

    async def run(self) -> None:
        while not self.stopped:
            self.wake.clear()
            with self.database.connect() as db:
                row = db.execute(
                    "SELECT * FROM outbox WHERE delivered=0 AND attempts<5 ORDER BY due_at LIMIT 1"
                ).fetchone()
            if row and row["due_at"] <= time.time():
                job = self.database.get_job(row["job_id"])
                if job is None:
                    continue
                begin = time.monotonic_ns()
                try:
                    await self.send(job, row["text"])
                except Exception as exc:
                    attempts = row["attempts"] + 1
                    with self.database.connect() as db:
                        db.execute(
                            "UPDATE outbox SET attempts=?,due_at=? WHERE job_id=?",
                            (attempts, time.time() + min(60, 2**attempts), job.id),
                        )
                    self.database.record_event(
                        "outbox_failed",
                        job_id=job.id,
                        details={"attempt": attempts, "error_type": type(exc).__name__},
                    )
                else:
                    with self.database.connect() as db:
                        db.execute("UPDATE outbox SET delivered=1 WHERE job_id=?", (job.id,))
                    with self.database.connect() as db:
                        receipt = db.execute(
                            "SELECT details FROM interaction_events WHERE job_id=? "
                            "AND event='performance_accepted' ORDER BY id LIMIT 1",
                            (job.id,),
                        ).fetchone()
                    timing = json.loads(receipt[0]) if receipt else {}
                    total = (
                        (time.monotonic_ns() - timing["received_ns"]) / 1e6
                        if timing.get("boot_id") == BOOT_ID
                        else None
                    )
                    self.database.record_event(
                        "performance_delivery",
                        job_id=job.id,
                        details={
                            "receipt_to_reply_ms": total,
                            "delivery_ms": (time.monotonic_ns() - begin) / 1e6,
                            "attempt": row["attempts"] + 1,
                        },
                    )
                continue
            timeout = max(0.0, row["due_at"] - time.time()) if row else None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout)

    async def stop(self) -> None:
        self.stopped = True
        self.wake.set()
