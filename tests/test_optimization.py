from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from home_agent.codex_runtime import CodexResult
from home_agent.config import Settings
from home_agent.database import Database
from home_agent.optimization import export_interactions, review


def test_event_history_preserves_attempts_responses_and_redacts_tokens(settings: Settings) -> None:
    db = Database(settings.database_path)
    db.initialize()
    token = "123456789:" + "a" * 35
    job = db.enqueue("telegram", "question " + token, telegram_update_id=1)
    assert job
    db.set_ack_message(job.id, 10)
    db.claim_next()
    db.finish(job.id, "failed", "first failure")
    db.retry(job.id)
    db.claim_next()
    db.mark_codex_started(job.id)
    db.finish(job.id, "completed", response="second response")
    db.initialize()
    with db.connect() as conn:
        events = [dict(row) for row in conn.execute("SELECT * FROM interaction_events ORDER BY id")]
    assert [e["event"] for e in events] == [
        "queued",
        "acknowledged",
        "running",
        "failed",
        "retry_requested",
        "running",
        "codex_started",
        "completed",
    ]
    assert all(datetime.fromisoformat(e["occurred_at"]).tzinfo for e in events)
    assert "first failure" in events[3]["details"]
    assert "second response" in events[-1]["details"]
    assert token not in json.dumps(events)
    assert "[REDACTED]" in events[0]["details"]


def test_deployment_pause_preserves_queue_until_resumed(settings: Settings) -> None:
    db = Database(settings.database_path)
    db.initialize()
    first = db.enqueue("telegram", "first")
    db.claim_next()
    db.set_metadata("deployment_paused", True)
    second = db.enqueue("telegram", "second")
    assert first and second
    db.finish(first.id, "completed")
    assert db.claim_next() is None
    assert db.get_job(second.id).status == "queued"  # type: ignore[union-attr]
    db.set_metadata("deployment_paused", False)
    assert db.claim_next().id == second.id  # type: ignore[union-attr]


@pytest.mark.parametrize(("date", "hours"), [("2026-03-09", 23), ("2026-11-02", 25)])
def test_report_uses_local_calendar_day_and_preserves_cross_midnight_history(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    date: str,
    hours: int,
) -> None:
    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return cls.fromisoformat(date + "T00:00:00")

    monkeypatch.setattr("home_agent.optimization.datetime", Clock)
    try:
        db = Database(settings.database_path)
        db.initialize()
        start = "2026-03-08T07:59:00+00:00" if hours == 23 else "2026-11-01T06:59:00+00:00"
        finish = "2026-03-08T08:01:00+00:00" if hours == 23 else "2026-11-01T07:01:00+00:00"
        monkeypatch.setattr("home_agent.database.utc_now", lambda: start)
        job = db.enqueue("telegram", "cross midnight")
        assert job
        db.claim_next()
        monkeypatch.setattr("home_agent.database.utc_now", lambda: finish)
        db.finish(job.id, "completed", response="answer")
        report = tmp_path / "report"
        report.mkdir()
        summary = export_interactions(settings, report)
        delta = datetime.fromisoformat(summary["end_exclusive"]) - datetime.fromisoformat(
            summary["start_inclusive"]
        )
        assert delta.total_seconds() == hours * 3600
        assert summary["jobs"] == 1
        assert "cross midnight" in (report / "jobs.jsonl").read_text()
        assert "queued" in (report / "job-history.jsonl").read_text()
        assert (report / "jobs.jsonl").stat().st_mode & 0o777 == 0o600
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


@pytest.mark.asyncio
async def test_daily_review_is_separate_private_and_runs_once(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    settings = replace(settings, optimization_checkout=checkout)
    (settings.durable_dir / "optimization.md").write_text("Review evidence and use normal CI.")
    calls: list[str] = []

    class Runtime:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert kwargs["model"] == settings.optimization_model

        async def run(self, prompt: str, **kwargs: Any) -> CodexResult:
            assert kwargs["thread_id"] is None
            calls.append(prompt)
            return CodexResult("review-thread", "NOOP: insufficient data")

        async def close(self) -> None:
            pass

    monkeypatch.setattr("home_agent.optimization.CodexRuntime", Runtime)
    assert await review(settings, report_only=True) == 0
    assert not calls
    assert await review(settings) == 0
    assert await review(settings) == 0
    assert len(calls) == 1
    db = Database(settings.database_path)
    assert db.snapshot().queued == 0
    assert db.get_thread("telegram") is None
    reports = settings.data_dir / "optimization"
    assert len(list(reports.glob("*/response.md"))) == 1
    assert reports.stat().st_mode & 0o777 == 0o700
    with (reports / "review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert await review(settings, report_only=True) == 0
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_review_is_recorded_and_not_replayed(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    settings = replace(settings, optimization_checkout=checkout)
    (settings.durable_dir / "optimization.md").write_text("Review privately.")
    calls = []

    class Runtime:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self, *args: Any, **kwargs: Any) -> CodexResult:
            calls.append(True)
            raise RuntimeError("interrupted after possible publication")

        async def close(self) -> None:
            pass

    monkeypatch.setattr("home_agent.optimization.CodexRuntime", Runtime)
    with pytest.raises(RuntimeError):
        await review(settings)
    assert await review(settings) == 1
    assert len(calls) == 1
    records = list((settings.data_dir / "optimization").glob("*/result.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["status"] == "failed_or_uncertain"
