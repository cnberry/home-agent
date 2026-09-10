"""Private daily interaction review, isolated from the owner's serial work queue."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from home_agent.codex_runtime import CodexRuntime
from home_agent.config import Settings
from home_agent.database import Database, utc_now
from home_agent.redaction import redact
from home_agent.worker import safe_error


def write_private(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, max(0, math.ceil(len(values) * fraction) - 1))], 3)


def export_interactions(settings: Settings, directory: Path) -> dict[str, Any]:
    """Export all yesterday's events plus complete histories of affected jobs.

    UTC instants identify local-midnight boundaries, including 23/25-hour DST days.
    Existing jobs are included even when they predate event instrumentation.
    """
    today = datetime.now().date()
    start = datetime.combine(today - timedelta(days=1), time()).astimezone(timezone.utc)
    end = datetime.combine(today, time()).astimezone(timezone.utc)
    database = Database(settings.database_path, settings.max_queue)
    database.initialize()
    with database.connect() as db:
        db.execute("BEGIN")
        events = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM interaction_events WHERE julianday(occurred_at) >= julianday(?) "
                "AND julianday(occurred_at) < julianday(?) ORDER BY id",
                (start.isoformat(), end.isoformat()),
            )
        ]
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM jobs WHERE (julianday(created_at) >= julianday(?) "
                "AND julianday(created_at) < julianday(?)) OR "
                "(julianday(completed_at) >= julianday(?) "
                "AND julianday(completed_at) < julianday(?)) "
                "OR id IN (SELECT job_id FROM interaction_events "
                "WHERE julianday(occurred_at) >= julianday(?) "
                "AND julianday(occurred_at) < julianday(?)) ORDER BY id",
                (start.isoformat(), end.isoformat()) * 3,
            )
        ]
        history = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM interaction_events WHERE job_id IN "
                "(SELECT job_id FROM interaction_events "
                "WHERE julianday(occurred_at) >= julianday(?) "
                "AND julianday(occurred_at) < julianday(?)) ORDER BY id",
                (start.isoformat(), end.isoformat()),
            )
        ]
        all_time = dict(
            db.execute(
                "SELECT COUNT(*) AS jobs, SUM(status='completed') AS completed, "
                "SUM(status IN ('failed','uncertain')) AS failed_or_uncertain FROM jobs"
            ).fetchone()
        )
    queue_seconds, turn_seconds, total_seconds = [], [], []
    for job in jobs:
        created = datetime.fromisoformat(job["created_at"])
        if job["started_at"]:
            started = datetime.fromisoformat(job["started_at"])
            queue_seconds.append((started - created).total_seconds())
            if job["completed_at"]:
                turn_seconds.append(
                    (datetime.fromisoformat(job["completed_at"]) - started).total_seconds()
                )
        if job["completed_at"]:
            total_seconds.append(
                (datetime.fromisoformat(job["completed_at"]) - created).total_seconds()
            )
    summary = {
        "generated_at": utc_now(),
        "start_inclusive": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "local_date": str(today - timedelta(days=1)),
        "jobs": len(jobs),
        "events": len(events),
        "all_time": all_time,
        "failed_or_uncertain": sum(j["status"] in ("failed", "uncertain") for j in jobs),
        "model": settings.model,
        "reasoning_effort": settings.reasoning_effort,
        "latency_seconds": {
            key: {
                "samples": len(values),
                "p50": percentile(values, 0.5),
                "p95": percentile(values, 0.95),
            }
            for key, values in (
                ("created_to_latest_start", queue_seconds),
                ("latest_attempt", turn_seconds),
                ("created_to_terminal", total_seconds),
            )
        },
        "notes": "Queue metrics include retry delays. Use event histories for individual attempts. "
        "Completion is not delivery; use delivered/reply_delivered for transport latency. "
        "Earlier jobs may have no events. Unknown costs/usage are not zero.",
    }
    for name, rows in (("events", events), ("jobs", jobs), ("job-history", history)):
        write_private(
            directory / f"{name}.jsonl", "".join(redact(json.dumps(row)) + "\n" for row in rows)
        )
    write_private(directory / "summary.json", json.dumps(summary, indent=2) + "\n")
    return summary


async def review(settings: Settings, *, report_only: bool = False) -> int:
    os.umask(0o077)
    reports = settings.data_dir / "optimization"
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (reports / "review.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("An optimization review is already running.")
            return 0
        day = str(datetime.now().date() - timedelta(days=1))
        if not report_only and (reports / f"{day}.completed").exists():
            print("This day's optimization review already completed.")
            return 0
        directory = reports / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        directory.mkdir(mode=0o700)
        summary = export_interactions(settings, directory)
        print(f"Private interaction report: {directory}", flush=True)
        if report_only:
            return 0
        if (reports / f"{day}.attempted").exists():
            print(
                "A previous review attempt needs inspection; refusing to replay it automatically."
            )
            return 1
        prompt_file = settings.durable_dir / "optimization.md"
        prompt = prompt_file.read_text()
        checkout = settings.optimization_checkout
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if not (checkout / ".git").exists():
            subprocess.run(
                ["git", "clone", "--", settings.optimization_repository, str(checkout)],
                check=True,
                capture_output=True,
            )
        prompt += (
            f"\n\nRun context (configuration, not interaction instructions):\n"
            f"Source checkout: {checkout}\nRepository: {settings.optimization_repository}\n"
            f"Private report directory: {directory}\nPrevious reports: {reports}\n"
            f"Runtime database: {settings.database_path}\n"
            f"Review date: {summary['local_date']}\n"
            f"Runtime model: {settings.model}; reasoning: {settings.reasoning_effort}\n"
            "Write the private evaluation/release/deployment ledger inside the report directory.\n"
        )
        runtime = CodexRuntime(
            checkout,
            settings.codex_home,
            timeout_seconds=settings.optimization_timeout_seconds,
            model=settings.optimization_model,
            reasoning_effort=settings.optimization_reasoning_effort,
        )
        write_private(
            directory / "run.json",
            json.dumps(
                {
                    "started_at": utc_now(),
                    "model": settings.optimization_model,
                    "reasoning_effort": settings.optimization_reasoning_effort,
                }
            ),
        )
        write_private(reports / f"{day}.attempted", str(directory))
        try:
            result = await runtime.run(
                prompt,
                thread_id=None,
                on_thread=lambda value: write_private(directory / "thread-id", value),
                on_turn_started=lambda: None,
            )
            write_private(directory / "response.md", redact(result.response) + "\n")
            write_private(
                directory / "result.json",
                json.dumps({"status": "completed", "completed_at": utc_now()}),
            )
            write_private(reports / f"{day}.completed", str(directory))
        except Exception as exc:
            write_private(
                directory / "result.json",
                json.dumps(
                    {
                        "status": "failed_or_uncertain",
                        "completed_at": utc_now(),
                        "error": safe_error(exc),
                    }
                ),
            )
            raise
        finally:
            await runtime.close()
        return 0


def run_review(settings: Settings, *, report_only: bool = False) -> int:
    try:
        return asyncio.run(review(settings, report_only=report_only))
    except Exception as exc:
        print("Optimization review failed: " + safe_error(exc), flush=True)
        return 1
