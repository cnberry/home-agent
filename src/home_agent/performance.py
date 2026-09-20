"""Private, versioned timing evidence; no prompts or credentials in summaries."""

from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from home_agent import __version__
from home_agent.database import Database, Job

BOOT_ID = (
    Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if Path("/proc/sys/kernel/random/boot_id").exists()
    else "unknown"
)


def accepted(database: Database, job: Job, received_ns: int, source: str) -> None:
    with database.connect() as db:
        if db.execute(
            "SELECT 1 FROM interaction_events WHERE job_id=? AND event='performance_accepted'",
            (job.id,),
        ).fetchone():
            return
    database.record_event(
        "performance_accepted",
        job_id=job.id,
        details={
            "schema_version": 1,
            "received_ns": received_ns,
            "boot_id": BOOT_ID,
            "accept_ms": (time.monotonic_ns() - received_ns) / 1e6,
            "source": source,
        },
    )


class Performance:
    def __init__(self, database: Database, job: Job):
        self.database, self.job = database, job
        self.started_ns = time.monotonic_ns()
        self.received_ns: int | None = None
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "attempt": job.attempts,
            "engine": "codex",
            "eligible": False,
            "release": os.environ.get("HOME_AGENT_REVISION", __version__),
            "boot_id": BOOT_ID,
        }
        with database.connect() as db:
            row = db.execute(
                "SELECT details FROM interaction_events WHERE job_id=? AND "
                "event='performance_accepted' ORDER BY id DESC LIMIT 1",
                (job.id,),
            ).fetchone()
        if row:
            value = json.loads(row["details"])
            self.data.update({k: value[k] for k in ("accept_ms", "source")})
            if value["boot_id"] == BOOT_ID:
                self.received_ns = value["received_ns"]
        self.data["queue_ms"] = (
            (self.started_ns - self.received_ns) / 1e6
            if self.received_ns
            else max(
                0,
                (
                    datetime.now(timezone.utc) - datetime.fromisoformat(job.created_at)
                ).total_seconds()
                * 1000,
            )
        )

    def event(self, name: str, **fields: Any) -> None:
        now = time.monotonic_ns()
        self.database.record_event(
            name,
            job_id=self.job.id,
            details={
                "schema_version": 1,
                "attempt": self.job.attempts,
                "monotonic_ns": now,
                "boot_id": BOOT_ID,
                "elapsed_ms": (now - self.started_ns) / 1e6,
                **fields,
            },
        )

    def issued(self, monotonic_ns: int) -> None:
        self.data["driver_issue_ms"] = (
            (monotonic_ns - self.received_ns) / 1e6 if self.received_ns else None
        )
        self.data["driver_issue_from_claim_ms"] = (monotonic_ns - self.started_ns) / 1e6
        self.event("driver_issued", **self.data)

    def finish(self) -> None:
        job = self.database.get_job(self.job.id)
        if job is None:
            return
        self.data.update(
            status=job.status,
            completed_ms=(time.monotonic_ns() - self.received_ns) / 1e6
            if self.received_ns
            else None,
            processing_ms=(time.monotonic_ns() - self.started_ns) / 1e6,
        )
        self.event("performance", **self.data)
        logging.getLogger(__name__).info(
            "performance job_id=%s %s", self.job.id, json.dumps(self.data)
        )


def report(
    database: Database, since: str = "1970-01-01", source: str | None = None
) -> dict[str, Any]:
    with database.connect() as db:
        rows = db.execute(
            "SELECT e.job_id,e.details,j.status FROM interaction_events e JOIN jobs j "
            "ON j.id=e.job_id WHERE e.event IN ('performance_accepted','performance') "
            "AND e.occurred_at>=? ORDER BY e.id",
            (since,),
        ).fetchall()
    # Retry attempts remain in raw evidence; job-level report uses the latest outcome.
    latest: dict[int, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["job_id"], {"eligible": True}).update(json.loads(row["details"]))
        latest[row["job_id"]]["status"] = row["status"]
    values = [r for r in latest.values() if source is None or r.get("source") == source]
    groups: dict[str, Any] = {}
    for key in sorted({f"{r.get('engine')}/{r.get('capability', 'general')}" for r in values}):
        group = [r for r in values if f"{r.get('engine')}/{r.get('capability', 'general')}" == key]
        terminal = [
            r
            for r in group
            if r.get("eligible")
            and r["status"] in ("completed", "failed", "uncertain", "cancelled")
        ]
        success = sum(
            r["status"] == "completed" and r.get("outcome") in ("confirmed", "observed")
            for r in terminal
        )
        issue = sorted(
            r["driver_issue_ms"]
            for r in terminal
            if isinstance(r.get("driver_issue_ms"), (float, int))
        )
        commands = [r for r in terminal if r.get("mutation") is True]
        groups[key] = {
            "jobs": len(group),
            "eligible_terminal": len(terminal),
            "successes": success,
            "success_rate": success / len(terminal) if terminal else None,
            "command_jobs": len(commands),
            "issue_samples": len(issue),
            "missing_issue_timings": sum(r.get("driver_issue_ms") is None for r in commands),
            "issue_under_1s": sum(x < 1000 for x in issue),
            "issue_p50_ms": issue[math.ceil(0.5 * len(issue)) - 1] if issue else None,
            "issue_p95_ms": issue[math.ceil(0.95 * len(issue)) - 1] if issue else None,
            "clarifications": sum(r.get("outcome") == "clarify" for r in group),
            "fallbacks": sum(bool(r.get("fallback_reason")) for r in group),
        }
        for metric in ("accept_ms", "queue_ms", "inference_ms", "device_ms", "completed_ms"):
            samples = sorted(r[metric] for r in group if isinstance(r.get(metric), (int, float)))
            groups[key][metric] = {
                "samples": len(samples),
                "p50": samples[math.ceil(0.5 * len(samples)) - 1] if samples else None,
                "p95": samples[math.ceil(0.95 * len(samples)) - 1] if samples else None,
                "max": max(samples) if samples else None,
            }
    return {
        "schema_version": 1,
        "since": since,
        "source": source,
        "goals": {"success_rate_gt": 0.99, "driver_issue_ms_lt": 1000},
        "jobs": len(values),
        "groups": groups,
        "incomplete": sum(r["status"] in ("queued", "running") for r in values),
        "missing_final_evidence": sum("outcome" not in r for r in values),
        "note": "Driver issue means CLI process launched, not hardware "
        "acknowledgement. Missing timings are not passes. Small samples do not establish the SLO.",
    }
